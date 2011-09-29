from streamsem import events


class Filter(object):
    def __init__(self, callback):
        """Creates a new filter.

        `callback`: function to call for every event matchinf the filter.

        """
        self.callback = callback

    def filter_event(self, event):
        """The filter executes the callback for every matching event.

        This implementation just filters out all the events.

        """
        pass

    def filter_events(self, events):
        """Convenience method when an iterable of events is available.

        """
        for event in events:
            self.filter_event(event)


class SourceFilter(Filter):
    def __init__(self, callback, source_id=None, source_ids=[]):
        """Creates a filter for source ids.

        `source_id` must be only one id, whereas `source_ids` must be
        a list of ids. If both are present, `source_id`is appended to
        the list of ids.

        """
        super(SourceFilter, self).__init__(callback)
        self.source_ids = set()
        if source_id is not None:
            self.source_ids.add(source_id)
        for source in source_ids:
            self.source_ids.add(source)

    def filter_event(self, event):
        if event.source_id in self.source_ids:
            self.callback(event)


class ApplicationFilter(Filter):
    def __init__(self, callback, application_id=None, application_ids=[]):
        """Creates a filter for application ids.

        `application_id` must be only one id, whereas
        `application_ids` must be a list of ids. If both are present,
        `application_id`is appended to the list of ids.

        """
        super(ApplicationFilter, self).__init__(callback)
        self.application_ids = set()
        if application_id is not None:
            self.application_ids.add(application_id)
        for application in application_ids:
            self.application_ids.add(application)

    def filter_event(self, event):
        if event.application_id in self.application_ids:
            self.callback(event)
