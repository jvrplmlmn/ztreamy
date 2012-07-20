# streamsem: a framework for publishing semantic events on the Web
# Copyright (C) 2011-2012 Jesus Arias Fisteus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
""" Clients that communicate with stream servers to send or receive events.

There are several clients that receive events: 'Client',
'AsyncStreamingClient' and 'SynchronousClient'. Both 'Client' and
'AsyncStreamingClient' are asynchronous. Their difference is that
'Client' can listen to several event streams at the same time. It is
implemented as a wrapper on top of 'AsyncStreamingClient'. On the
other hand, 'SynchronousClient' implements a synchronous client for
just one stream.

'EventPublisher' is an asynchronous class that sends events to be
served in a stream. 'SynchronousEventPublisher' has a similar
interface, but is synchronous.

"""
import tornado.ioloop
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from tornado.curl_httpclient import CurlAsyncHTTPClient
import tornado.options
import logging
import zlib
import sys
import urllib2
import httplib
from urlparse import urlparse
import datetime

import streamsem
from streamsem import Deserializer, Command, mimetype_event
from streamsem import logger

transferred_bytes = 0
data_count = 0

#AsyncHTTPClient.configure("tornado.simple_httpclient.SimpleAsyncHTTPClient")
AsyncHTTPClient.configure("tornado.curl_httpclient.CurlAsyncHTTPClient",
                          max_clients=32768)

class Client(object):
    """Asynchronous client for multiple stream sources.

    This client is able to receive events from multiple streams. Its
    internal implementation is based on the 'AsyncStreamingClient'
    class.

    """
    def __init__(self, source_urls, event_callback, error_callback=None,
                 ioloop=None, parse_event_body=True, separate_events=True):
        """Creates a new client for one or more stream URLs.

        The client connects to the stream URLs in the list
        'source_urls' (although a single string is also accepted).
        For every single received event, the 'event_callback' function
        is invoked. It receives an event object as parameter.

        If 'separate_events' is set to None, then the event callback
        will receive a list of events instead of a single events.

        If a 'ioloop' object is given, the client will block on it
        apon calling the 'start()' method. If not, it will block on
        the default 'ioloop' of Tornado.

        """
        if isinstance(source_urls, basestring):
            self.source_urls = [source_urls]
        else:
            self.source_urls = source_urls
        self.clients = \
            [AsyncStreamingClient(url, event_callback=event_callback,
                         error_callback=error_callback,
                         connection_close_callback=self._client_close_callback,
                         parse_event_body=parse_event_body,
                         separate_events=separate_events) \
                 for url in self.source_urls]
        self.ioloop = ioloop or tornado.ioloop.IOLoop.instance()
        self._closed = False
        self._looping = False
        self.active_clients = []

    def start(self, loop=True):
        """Starts the client.

        This function has to be called in order to connect to the
        streams and begin to receive events.

        If 'loop' is true (which is the default), the server will
        block on the ioloop until 'close()' is called.

        """
        if self._closed:
            raise Exception('This client has already been closed.')
        for client in self.clients:
            client.start(False)
            self.active_clients.append(client)
        if loop:
            self._looping = True
            self.ioloop.start()
            self._looping = False

    def stop(self):
        """Stops and closes this client.

        The client can no longer be used in the future.

        If the server is blocked on the ioloop in the 'start()'
        method, it is released.

        """
        if not self._closed:
            for client in self.clients:
                client.stop()
        self.active_clients = []
        self._closed = True
        if self._looping:
            self.ioloop.stop()
            self._looping = False

    def _client_close_callback(self, client):
        if client in self.active_clients:
            self.active_clients.remove(client)
            if len(self.active_clients) == 0 and self._looping:
                self.ioloop.stop()
                self._looping = False


class AsyncStreamingClient(object):
    """Asynchronous client for a single event source.

    If you need to receive events from several sources, use the class
    'Client' instead.

    """
    def __init__(self, url, event_callback=None, error_callback=None,
                 connection_close_callback=None,
                 ioloop=None, parse_event_body=True, separate_events=True):
        """Creates a new client for a given stream URLs.

        The client connects to the stream URL given by 'url'.  For
        every single received event, the 'event_callback' function is
        invoked. It receives an event object as parameter.

        If 'separate_events' is set to None, then the event callback
        will receive a list of events instead of a single events.

        If a 'ioloop' object is given, the client will block on it
        apon calling the 'start()' method. If not, it will block on
        the default 'ioloop' of Tornado.

        """
        self.url = url
        self.event_callback = event_callback
        self.error_callback = error_callback
        self.connection_close_callback = connection_close_callback
        self.ioloop = ioloop or tornado.ioloop.IOLoop.instance()
        self.parse_event_body = parse_event_body
        self.separate_events = separate_events
        self._closed = False
        self._looping = False
        self._compressed = False
        self._deserializer = Deserializer()
        self.last_event = None
        self.connection_attempts = 0
#        self.data_history = []

    def start(self, loop=False):
        """Starts the client.

        This function has to be called in order to connect to the
        streams and begin to receive events.

        If 'loop' is True (the default is False), the server will
        block on the ioloop until 'close()' is called.

        """
        self._connect()
        if loop:
            self._looping = True
            self.ioloop.start()
            self._looping = False

    def stop(self):
        """Stops and closes this client.

        The client can no longer be used in the future.

        If the server is blocked on the ioloop in the 'start()'
        method, it is released.

        Note: if the backend behind
        'tornado.httpclient.AsyncHTTPClient()' is 'SimpleHTTPClient',
        invoking 'stop()' does not actually close the HTTP connections
        (as of Tornado branch master september 1st 2011).

        """
        if not self._closed:
            ## self.http_client.close()
            self._finish_internal(False)

    def _connect(self):
        http_client = AsyncHTTPClient()
        if self.last_event is None:
            url = self.url
        else:
            url = self.url + '?last-seen=' + self.last_event
        req = HTTPRequest(url, streaming_callback=self._stream_callback,
                          request_timeout=0, connect_timeout=0)
        http_client.fetch(req, self._request_callback)
        self.connection_attempts += 1

    def _reconnect(self):
        logging.info('Reconnecting to the stream...')
        self.ioloop.add_timeout(datetime.timedelta(seconds=5), self._connect)

    def _finish_internal(self, notify_connection_close):
        if (notify_connection_close
            and self.connection_close_callback is not None):
            self.connection_close_callback(self)
        if self._looping:
            self.ioloop.stop()
            self._looping = False

    def _stream_callback(self, data):
        global transferred_bytes
        self.connection_attempts = 0
        transferred_bytes += len(data)
        evs = self._deserialize(data, parse_body=self.parse_event_body)
        for e in evs:
            logger.logger.event_delivered(e)
        if self.event_callback is not None:
            if not self.separate_events:
                self.event_callback(evs)
            else:
                for ev in evs:
                    self.event_callback(ev)
        if len(evs) > 0:
            self.last_event = evs[-1].event_id

    def _request_callback(self, response):
        if response.error:
            if self.connection_attempts < 5:
                self._reconnect()
                finish = False
            else:
                if self.error_callback is not None:
                    self.error_callback('Error in HTTP request',
                                        http_error=response.error)
                finish = True
        elif len(response.body) > 0:
#            self.data_history.append(response.body)
            self._notify_event(response.body)
            finish = True
        if finish:
            logging.info('Finishing client')
            self._finish_internal(True)

    def _reset_compression(self):
        self._compressed = True
        self._decompressor = zlib.decompressobj()

    def _deserialize(self, data, parse_body=True):
        evs = []
        event = None
        compressed_len = len(data)
        if self._compressed:
            data = self._decompressor.decompress(data)
        logger.logger.data_received(compressed_len, len(data))
        self._deserializer.append_data(data)
        event = self._deserializer.deserialize_next(parse_body=parse_body)
        while event is not None:
            if isinstance(event, Command):
                if event.command == 'Set-Compression':
                    self._reset_compression()
                    pos = self._deserializer.data_consumed()
                    self._deserializer.reset()
                    evs.extend(self._deserialize(data[pos:], parse_body))
                    return evs
                elif event.command == 'Stream-Finished':
                    self._finish_internal(True)
                    ## logging.info('Stream finished')
            else:
                evs.append(event)
            event = self._deserializer.deserialize_next(parse_body=parse_body)
        return evs


class SynchronousClient(object):
    """Synchronous client.

    This client should be used in short-lived mode.

    """
    def __init__(self, server_url, parse_event_body=True,
                 last_event_seen=None):
        self.server_url = server_url
        self.last_event_seen = last_event_seen
        self.deserializer = Deserializer()
        self.parse_event_body = parse_event_body
        self.stream_finished = False

    def receive_events(self):
        url = self.server_url
        if self.last_event_seen is not None:
            url += '?last-seen=' + self.last_event_seen
        connection = urllib2.urlopen(url)
        data = connection.read()
        evs = self.deserializer.deserialize(data, complete=True,
                                            parse_body=self.parse_event_body)
        connection.close()
        if len(evs) > 0:
            self.last_event_seen = evs[-1].event_id
        for event in evs:
            if (isinstance(event, Command)
                and event.command == 'Stream-Finished'):
                self.stream_finished = True
                break
        return [e for e in evs if not isinstance(e, Command)]


class EventPublisher(object):
    """Publishes events by sending them to a server. Asynchronous.

    Uses an asynchronous HTTP client, but does not manage an ioloop
    itself. The ioloop must be run by the calling code.

    """
    def __init__(self, server_url, io_loop=None):
        """Creates a new 'EventPublisher' object.

        Events are sent in separate HTTP requests to the server given
        by 'server_url'.

        """
        self.server_url = server_url
        self.http_client = CurlAsyncHTTPClient(io_loop=io_loop)
        self.headers = {'Content-Type': mimetype_event}
        self.ioloop = io_loop or tornado.ioloop.IOLoop.instance()

    def publish(self, event, callback=None):
        """Publishes a new event.

        The event is sent to the server in a new HTTP request. If a
        'callback' is given, it will be called when the response is
        received from the server. The callback receives a
        tornado.httpclient.HTTPResponse parameter.

        """
        logger.logger.event_published(event)
        self.publish_events([event], callback=callback)

    def publish_events(self, events, callback=None):
        """Publishes a list of events.

        The events in the list 'events' are sent to the server in a
        new HTTP request. If a 'callback' is given, it will be called
        when the response is received from the server. The callback
        receives a tornado.httpclient.HTTPResponse parameter.

        """
        body = streamsem.serialize_events(events)
        req = HTTPRequest(self.server_url, body=body, method='POST',
                          headers=self.headers, request_timeout=0,
                          connect_timeout=0)
        callback = callback or self._request_callback
        # Enqueue a new callback in the ioloop, to avoid problems
        # when this code is run from a callback of the HTTP client
        def fetch():
            self.http_client.fetch(req, callback)
        self.ioloop.add_callback(fetch)

    def close(self):
        """Closes the event publisher.

        This object should not be used anymore.

        """
        ## self.http_client.close()
        self.http_client=None

    def _request_callback(self, response):
        if response.error:
            logging.error(response.error)
        else:
            logging.info('Event successfully sent to server')


class SynchronousEventPublisher(object):
    """Publishes events by sending them to a server. Synchronous.

    Uses a synchronous HTTP client.

    """
    _headers = {'Content-Type': mimetype_event}

    def __init__(self, server_url, io_loop=None):
        """Creates a new 'SynchronousEventPublisher' object.

        Events are sent in separate HTTP requests to the server given
        by 'server_url'.

        """
        url_parts = urlparse(server_url)
        assert url_parts.scheme == 'http'
        self.hostname = url_parts.hostname
        self.port = url_parts.port or 80
        self.path = url_parts.path
        if url_parts.query is not None:
            self.path += '?' + url_parts.query

    def publish(self, event):
        """Publishes a new event.

        The event is sent to the server in a new HTTP request. Returns
        True if the data is received correctly by the server.

        """
        self.publish_events([event])

    def publish_events(self, events, callback=None):
        """Publishes a list of events.

        The events in the list 'events' are sent to the server in a
        new HTTP request. If a 'callback' is given, it will be called
        when the response is received from the server. The callback
        receives a tornado.httpclient.HTTPResponse parameter.

        """
        body = streamsem.serialize_events(events)
        conn = httplib.HTTPConnection(self.hostname, self.port)
        conn.request('POST', self.path, body,
                     SynchronousEventPublisher._headers)
        response = conn.getresponse()
        if response.status == 200:
            return True
        else:
            logging.error(str(response.status) + ' ' + response.reason)
            return False

    def close(self):
        """Closes the event publisher.

        It does nothing in this class, but is maintained for
        compatibility with the asynchronous publisher.

        """
        pass


def read_cmd_options():
    from optparse import OptionParser, Values
    tornado.options.define('eventlog', default=False,
                           help='dump event log',
                           type=bool)
    remaining = tornado.options.parse_command_line()
    options = Values()
    if len(remaining) >= 1:
        options.stream_urls = remaining
    else:
        OptionParser().error('At least one source stream URL required')
    return options

def main():
    import time
    def handle_event(event):
        sys.stdout.write(str(event))
    def handle_error(message, http_error=None):
        if http_error is not None:
            logging.error(message + ': ' + str(http_error))
        else:
            logging.error(message)
    def stop_client():
        client.stop()
    options = read_cmd_options()
#    import streamsem.filters
#    filter = streamsem.filters.SimpleTripleFilter(handle_event,
#                                        predicate='http://example.com/temp')
    client = Client(options.stream_urls,
                    event_callback=handle_event,
#                    event_callback=filter.filter_event,
                    error_callback=handle_error)
#    tornado.ioloop.IOLoop.instance().add_timeout(time.time() + 6, stop_client)
    node_id = streamsem.random_id()
    if tornado.options.options.eventlog:
        logger.logger = logger.StreamsemLogger(node_id,
                                               'client-' + node_id + '.log')
    try:
        client.start(loop=True)
    except KeyboardInterrupt:
        pass
    finally:
        logger.logger.close()

if __name__ == "__main__":
    main()
