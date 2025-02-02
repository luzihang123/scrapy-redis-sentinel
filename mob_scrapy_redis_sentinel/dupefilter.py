# -*- coding: utf-8 -*-
import logging
import time

import pymongo
from pymongo.errors import DuplicateKeyError

from scrapy.dupefilters import BaseDupeFilter
from scrapy.utils.request import request_fingerprint

from . import defaults
from .bloomfilter import BloomFilter
from .connection import from_settings

logger = logging.getLogger(__name__)


class MongoDupeFilter(BaseDupeFilter):
    def __init__(self, mongo_uri, db, collection, debug=False, *args, **kwargs):
        self.mongo = pymongo.MongoClient(mongo_uri)
        self.mongo_db = db
        self.collection = collection
        self.debug = debug
        self.logdupes = True
        self.mongo[self.mongo_db][self.collection].create_index("fp", unique=True)

    @classmethod
    def from_settings(cls, settings):
        mongo_uri = settings.get("MongoFilter_URI")
        mongo_db = settings.get("MongoFilter_DB")
        collection = defaults.DUPEFILTER_KEY % {"timestamp": int(time.time())}
        debug = settings.getbool("DUPEFILTER_DEBUG", False)
        return cls(mongo_uri=mongo_uri, db=mongo_db, collection=collection, debug=debug)

    @classmethod
    def from_crawler(cls, crawler):
        return cls.from_settings(crawler.settings)

    def request_seen(self, request):
        fp = self.request_fingerprint(request)
        # This returns the number of values added, zero if already exists.
        doc = self.mongo[self.mongo_db][self.collection].find_one({"fp": fp})
        if doc:
            return True
        try:
            self.mongo[self.mongo_db][self.collection].insert_one({"fp": fp})
        except DuplicateKeyError:
            pass
        return False

    def request_fingerprint(self, request):
        return request_fingerprint(request)

    @classmethod
    def from_spider(cls, spider):
        settings = spider.settings
        mongo_uri = settings.get("MongoFilter_URI")
        mongo_db = settings.get("MongoFilter_DB")
        dupefilter_key = settings.get("SCHEDULER_DUPEFILTER_KEY", defaults.SCHEDULER_DUPEFILTER_KEY)
        collection = dupefilter_key % {"spider": spider.name}
        debug = settings.getbool("DUPEFILTER_DEBUG")
        return cls(mongo_uri=mongo_uri, db=mongo_db, collection=collection, debug=debug)

    def close(self, reason=""):
        self.clear()

    def clear(self):
        self.mongo.drop_collection(self.collection)

    def log(self, request, spider):
        if self.debug:
            msg = "Filtered duplicate request: %(request)s"
            self.logger.debug(msg, {"request": request}, extra={"spider": spider})
        elif self.logdupes:
            msg = (
                "Filtered duplicate request %(request)s"
                " - no more duplicates will be shown"
                " (see DUPEFILTER_DEBUG to show all duplicates)"
            )
            self.logger.debug(msg, {"request": request}, extra={"spider": spider})
            self.logdupes = False

        spider.crawler.stats.inc_value("dupefilter/filtered", spider=spider)


# TODO: Rename class to RedisDupeFilter.
class RedisDupeFilter(BaseDupeFilter):
    """Redis-based request duplicates filter.

    This class can also be used with default Scrapy's scheduler.

    """

    logger = logger

    def __init__(self, server, key, debug=False, *args, **kwargs):
        """Initialize the duplicates filter.

        Parameters
        ----------
        server : redis.StrictRedis
            The redis server instance.
        key : str
            Redis key Where to store fingerprints.
        debug : bool, optional
            Whether to log filtered requests.

        """
        self.server = server
        self.key = key
        self.debug = debug
        self.logdupes = True

    @classmethod
    def from_settings(cls, settings):
        """Returns an instance from given settings.

        This uses by default the key ``dupefilter:<timestamp>``. When using the
        ``scrapy_redis.scheduler.Scheduler`` class, this method is not used as
        it needs to pass the spider name in the key.

        Parameters
        ----------
        settings : scrapy.settings.Settings

        Returns
        -------
        RFPDupeFilter
            A RFPDupeFilter instance.


        """
        server = from_settings(settings)
        # XXX: This creates one-time key. needed to support to use this
        # class as standalone dupefilter with scrapy's default scheduler
        # if scrapy passes spider on open() method this wouldn't be needed
        # TODO: Use SCRAPY_JOB env as default and fallback to timestamp.
        key = defaults.DUPEFILTER_KEY % {"timestamp": int(time.time())}
        debug = settings.getbool("DUPEFILTER_DEBUG")
        return cls(server, key=key, debug=debug)

    @classmethod
    def from_crawler(cls, crawler):
        """Returns instance from crawler.

        Parameters
        ----------
        crawler : scrapy.crawler.Crawler

        Returns
        -------
        RFPDupeFilter
            Instance of RFPDupeFilter.

        """
        return cls.from_settings(crawler.settings)

    def request_seen(self, request):
        """Returns True if request was already seen.

        Parameters
        ----------
        request : scrapy.http.Request

        Returns
        -------
        bool

        """
        fp = self.request_fingerprint(request)
        # This returns the number of values added, zero if already exists.
        added = self.server.sadd(self.key, fp)
        return added == 0

    def request_fingerprint(self, request):
        """Returns a fingerprint for a given request.

        Parameters
        ----------
        request : scrapy.http.Request

        Returns
        -------
        str

        """
        return request_fingerprint(request)

    @classmethod
    def from_spider(cls, spider):
        settings = spider.settings
        server = from_settings(settings)
        dupefilter_key = settings.get("SCHEDULER_DUPEFILTER_KEY", defaults.SCHEDULER_DUPEFILTER_KEY)
        key = dupefilter_key % {"spider": spider.name}
        debug = settings.getbool("DUPEFILTER_DEBUG")
        return cls(server, key=key, debug=debug)

    def close(self, reason=""):
        """Delete data on close. Called by Scrapy's scheduler.

        Parameters
        ----------
        reason : str, optional

        """
        self.clear()

    def clear(self):
        """Clears fingerprints data."""
        self.server.delete(self.key)

    def log(self, request, spider):
        """Logs given request.

        Parameters
        ----------
        request : scrapy.http.Request
        spider : scrapy.spiders.Spider

        """
        if self.debug:
            msg = "Filtered duplicate request: %(request)s"
            self.logger.debug(msg, {"request": request}, extra={"spider": spider})
        elif self.logdupes:
            msg = (
                "Filtered duplicate request %(request)s"
                " - no more duplicates will be shown"
                " (see DUPEFILTER_DEBUG to show all duplicates)"
            )
            self.logger.debug(msg, {"request": request}, extra={"spider": spider})
            self.logdupes = False

        spider.crawler.stats.inc_value("dupefilter/filtered", spider=spider)


class RedisBloomFilter(BaseDupeFilter):
    """Redis-based request duplicates filter.

    This class can also be used with default Scrapy's scheduler.

    """

    logger = logger

    def __init__(self, server, key, debug, bit, hash_number):
        """Initialize the duplicates filter.

        Parameters
        ----------
        server : redis.StrictRedis
            The redis server instance.
        key : str
            Redis key Where to store fingerprints.
        debug : bool, optional
            Whether to log filtered requests.

        """
        self.server = server
        self.key = key
        self.debug = debug
        self.logdupes = True
        self.bit = bit
        self.hash_number = hash_number
        self.bf = BloomFilter(server, self.key, bit, hash_number)

    @classmethod
    def from_settings(cls, settings):
        """Returns an instance from given settings.

        This uses by default the key ``dupefilter:<timestamp>``. When using the
        ``mob_scrapy_redis_sentinel.scheduler.Scheduler`` class, this method is not used as
        it needs to pass the spider name in the key.

        Parameters
        ----------
        settings : scrapy.settings.Settings

        Returns
        -------
        RFPDupeFilter
            A RFPDupeFilter instance.


        """
        server = from_settings(settings)
        # XXX: This creates one-time key. needed to support to use this
        # class as standalone dupefilter with scrapy's default scheduler
        # if scrapy passes spider on open() method this wouldn't be needed
        # TODO: Use SCRAPY_JOB env as default and fallback to timestamp.
        key = defaults.DUPEFILTER_KEY % {"timestamp": int(time.time())}
        debug = settings.getbool("DUPEFILTER_DEBUG", False)
        bit = settings.getint("BLOOMFILTER_BIT", 30)
        hash_number = settings.getint("BLOOMFILTER_HASH_NUMBER", 6)
        return cls(server=server, key=key, debug=debug, bit=bit, hash_number=hash_number)

    @classmethod
    def from_crawler(cls, crawler):
        """Returns instance from crawler.

        Parameters
        ----------
        crawler : scrapy.crawler.Crawler

        Returns
        -------
        RFPDupeFilter
            Instance of RFPDupeFilter.

        """
        return cls.from_settings(crawler.settings)

    def request_seen(self, request):
        """Returns True if request was already seen.

        Parameters
        ----------
        request : scrapy.http.Request

        Returns
        -------
        bool

        """
        fp = self.request_fingerprint(request)
        if self.bf.exists(fp):
            return True
        self.bf.insert(fp)
        return False

    def request_fingerprint(self, request):
        """Returns a fingerprint for a given request.

        Parameters
        ----------
        request : scrapy.http.Request

        Returns
        -------
        str

        """
        return request_fingerprint(request)

    def close(self, reason=""):
        """Delete data on close. Called by Scrapy's scheduler.

        Parameters
        ----------
        reason : str, optional

        """
        self.clear()

    def clear(self):
        """Clears fingerprints data."""
        self.server.delete(self.key)

    def log(self, request, spider):
        """Logs given request.

        Parameters
        ----------
        request : scrapy.http.Request
        spider : scrapy.spiders.Spider

        """
        if self.debug:
            msg = "Filtered duplicate request: %(request)s"
            self.logger.debug(msg, {"request": request}, extra={"spider": spider})
        elif self.logdupes:
            msg = (
                "Filtered duplicate request %(request)s"
                " - no more duplicates will be shown"
                " (see DUPEFILTER_DEBUG to show all duplicates)"
            )
            self.logger.debug(msg, {"request": request}, extra={"spider": spider})
            self.logdupes = False
        spider.crawler.stats.inc_value("bloomfilter/filtered", spider=spider)
