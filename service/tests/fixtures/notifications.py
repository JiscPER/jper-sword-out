from copy import deepcopy
from octopus.lib import dates

class NotificationFactory(object):

    @classmethod
    def notification_list(cls, since, page=1, pageSize=10, count=1):
        nl = deepcopy(NOTIFICATION_LIST)
        nl["since"] = since
        nl["page"] = page
        nl["pageSize"] = pageSize
        nl["total"]= count
        nl["timestamp"] = dates.now()
        for i in range(count):
            nl["notifications"].append(deepcopy(BASE_NOTIFICATION))
        return nl

    @classmethod
    def error_response(cls):
        return deepcopy(LIST_ERROR)

    @classmethod
    def unrouted_notification(cls):
        return deepcopy(BASE_NOTIFICATION)

LIST_ERROR = {
    "error" : "request failed"
}

BASE_NOTIFICATION = {
    "id" : "1234567890",
    "created_date" : "2015-02-02T00:00:00Z",

    "event" : "publication",

    "provider" : {
        "id" : "pub1",
        "agent" : "test/0.1",
        "ref" : "xyz",
        "route" : "api"
    },

    "content" : {
        "packaging_format" : "http://router.jisc.ac.uk/packages/FilesAndJATS",
        "store_id" : "abc"
    },

    "links" : [
        {
            "type" : "splash",
            "format" : "text/html",
            "access" : "public",
            "url" : "http://example.com/article/1"
        },
        {
            "type" : "fulltext",
            "format" : "application/pdf",
            "access" : "public",
            "url" : "http://example.com/article/1/pdf"
        }
    ],

    "embargo" : {
        "end" : "2016-01-01T00:00:00Z",
        "start" : "2015-01-01T00:00:00Z",
        "duration" : 12
    },

    "metadata" : {
        "title" : "Test Article",
        "version" : "AAM",
        "publisher" : "Premier Publisher",
        "source" : {
            "name" : "Journal of Important Things",
            "identifier" : [
                {"type" : "issn", "id" : "1234-5678" },
                {"type" : "eissn", "id" : "1234-5678" },
                {"type" : "pissn", "id" : "9876-5432" },
                {"type" : "doi", "id" : "10.pp/jit" }
            ]
        },
        "identifier" : [
            {"type" : "doi", "id" : "10.pp/jit.1" }
        ],
        "type" : "article",
        "author" : [
            {
                "name" : "Richard Jones",
                "identifier" : [
                    {"type" : "orcid", "id" : "aaaa-0000-1111-bbbb"},
                    {"type" : "email", "id" : "richard@example.com"},
                ],
                "affiliation" : "Cottage Labs, HP3 9AA"
            },
            {
                "name" : "Mark MacGillivray",
                "identifier" : [
                    {"type" : "orcid", "id" : "dddd-2222-3333-cccc"},
                    {"type" : "email", "id" : "mark@example.com"},
                ],
                "affiliation" : "Cottage Labs, EH9 5TP"
            }
        ],
        "language" : "eng",
        "publication_date" : "2015-01-01T00:00:00Z",
        "date_accepted" : "2014-09-01T00:00:00Z",
        "date_submitted" : "2014-07-03T00:00:00Z",
        "license_ref" : {
            "title" : "CC BY",
            "type" : "CC BY",
            "url" : "http://creativecommons.org/cc-by",
            "version" : "4.0",
        },
        "project" : [
            {
                "name" : "BBSRC",
                "identifier" : [
                    {"type" : "ringold", "id" : "bbsrcid"}
                ],
                "grant_number" : "BB/34/juwef"
            }
        ],
        "subject" : ["science", "technology", "arts", "medicine"]
    }
}

NOTIFICATION_LIST = {
    "since" : None,
    "page" : None,
    "pageSize" : None,
    "timestamp" : None,
    "total" : 1,
    "notifications" : []
}