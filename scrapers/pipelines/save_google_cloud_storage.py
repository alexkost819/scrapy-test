from google.cloud import storage
from scrapers.exporters import OpenStatesCloudStorageJsonItemExporter
from scrapy.utils.log import logger
from scrapy import signals
from .utils import clean_whitespace

STORAGE_SETTINGS_KEY = "SAVE_GOOGLE_CLOUD_STORAGE"


class SaveGoogleCloudStoragePipeline:
    def __init__(self, stats, google_cloud_settings, jurisdiction):
        self.stats = stats
        self.gcp_bucket = google_cloud_settings["bucket"]
        self.gcp_prefix = google_cloud_settings["prefix"]
        self.jurisdiction = jurisdiction
        self.cloud_storage_client = storage.Client()
        self.scrape_start_string = ""

    @classmethod
    def from_crawler(cls, crawler):
        google_cloud_settings = crawler.settings.get(STORAGE_SETTINGS_KEY, None)
        if google_cloud_settings is None:
            raise Exception(f"Settings must include {STORAGE_SETTINGS_KEY}")
        s = cls(crawler.stats, google_cloud_settings, crawler.spider.jurisdiction)
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def spider_opened(self, spider):
        logger.info('Spider opened: %s' % spider.name)
        self.scrape_start_string  = self.stats.get_value('start_time').isoformat()

    def save_object(self, obj, spider):
        clean_whitespace(obj)

        obj.pre_save(spider.jurisdiction)

        filename = f"{obj._type}_{obj._id}.json".replace("/", "-")
        jurisdiction_string = self.jurisdiction.division_id.replace("ocd-division/", "")
        storage_prefix = f"{self.gcp_prefix}/{jurisdiction_string}"
        if "legislative_session" in obj.__dict__:
            storage_path = f"{storage_prefix}/legislative_sessions/{obj.legislative_session}/{self.scrape_start_string}/{filename}"
        else:
            storage_path = f"{storage_prefix}/{self.scrape_start_string}/{filename}"

        logger.info(f"save {obj._type} as {filename}")

        exporter = OpenStatesCloudStorageJsonItemExporter(self.cloud_storage_client, self.gcp_bucket, storage_path)
        exporter.export_item(obj.as_dict())

    def process_item(self, item, spider):
        """
        Save object to disk as JSON.

        Generally shouldn't be called directly.
        """
        self.save_object(item, spider)
        # validate after writing, allows for inspection on failure
        try:
            item.validate()
        except ValueError as ve:
            logger.warn(ve)
        # after saving and validating, save subordinate objects
        for obj in item._related:
            self.save_object(obj, spider)

        return item
