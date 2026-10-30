from scrapers.exporters import OpenStatesJsonItemExporter
from scrapy.utils.log import logger
from scrapy import signals
import os
from .utils import clean_whitespace


class SaveLocalPipeline:
    def __init__(self, stats):
        self.stats = stats
        self.base_dir = '_data'

    @classmethod
    def from_crawler(cls, crawler):
        s = cls(crawler.stats)
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def spider_opened(self, spider):
        logger.info('Spider opened: %s' % spider.name)
        start_dt = self.stats.get_value('start_time').strftime('%Y-%m-%d')

        today_run_cnt = 1

        state_name = spider.name.replace('-bills', '')
        self.datadir = f"{self.base_dir}/{state_name}/{start_dt}"

        if os.path.exists(self.datadir):
            sub_dirs = [name for name in os.listdir(
                self.datadir) if os.path.isdir(os.path.join(self.datadir, name))]
            today_run_cnt = len(sub_dirs) + 1

        self.datadir = f"{self.datadir}/{today_run_cnt}"
        if not os.path.exists(self.datadir):
            os.makedirs(self.datadir)

    def save_object(self, obj, spider):
        clean_whitespace(obj)

        pre_save_method = getattr(obj, "pre_save", None)
        if callable(pre_save_method):
            obj.pre_save(spider.jurisdiction)

        filename = f"{obj._type}_{obj._id}.json".replace("/", "-")
        file_path = os.path.join(self.datadir, filename)

        logger.info(f"save {obj._type} as {filename}")

        with open(file_path, "w+b") as f:
            exporter = OpenStatesJsonItemExporter(f)
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
