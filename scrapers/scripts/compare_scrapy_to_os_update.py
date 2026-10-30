#!/usr/bin/env python3

from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import glob
import json
import logging
import os

from deepdiff import DeepDiff

# scraped_at is the save time, so it always differs between runs
KEYS_TO_IGNORE = ["_id", "scraped_at"]
logging.getLogger().setLevel(logging.DEBUG)


def cli_opts():
    parser = ArgumentParser(
        description="Compare output JSON from a Scrapy spider to output JSON from os-update scrape",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--scrapy-path",
        "-s",
        help="Path to the scrapy output JSON file for the specific scraper",
    )
    parser.add_argument(
        "--os-dir-path",
        "-o",
        help="Path to the os update scrape output directory for the specific scraper",
    )
    parser.add_argument(
        "--file-prefix",
        "-p",
        help="Text prefix to identify which files in the OS Update output dir we will include, ie 'bill'",
        default="bill",
    )
    return parser.parse_args()


def strip_ignored_keys(item):
    return dict([(key, val) for key, val in item.items() if key not in KEYS_TO_IGNORE])


def load_scrapy_json(filepath):
    with open(filepath, encoding="utf-8", mode="r") as file:
        data = json.load(file)

        # scrapy json file is a list of item dicts, which is what we want
        return data


def load_os_update_json_files_from_directory(directory_path, filter_prefix):
    items = []
    for filename in glob.glob(os.path.join(directory_path, f"{filter_prefix}*.json")):
        with open(filename, encoding="utf-8", mode="r") as file:
            data = json.load(file)
            # each Open States JSON file is an individual item dict, add to list
            items.append(data)

    return items


def find_matching_item_by_identifier(identifier, search_list):
    for item in search_list:
        if item["identifier"] == identifier:
            return item

    # we did not find a match
    return None


def diff_ignore_order_callback(level):
    # tell deepdiff to ignore the order of items in the versions list
    return 'versions' in level.path()


def main():
    args = cli_opts()
    logging.info(f"About to compare data in {args.scrapy_path} to {args.os_dir_path}")

    if os.path.isdir(args.scrapy_path):
        # SaveLocalPipeline output: one JSON file per entity, same layout as os-update
        scrapy_data = load_os_update_json_files_from_directory(args.scrapy_path, args.file_prefix)
    else:
        scrapy_data = load_scrapy_json(args.scrapy_path)
    os_scrape_data = load_os_update_json_files_from_directory(args.os_dir_path, args.file_prefix)

    if len(scrapy_data) != len(os_scrape_data):
        logging.error(f"Data counts do not match! scrapy: {len(scrapy_data)}, os: {len(os_scrape_data)}")

    for os_item in os_scrape_data:
        scrapy_item = find_matching_item_by_identifier(os_item["identifier"], scrapy_data)
        if scrapy_item is None:
            logging.error(f"Could not find matching item in Scrapy data for {os_item['identifier']}")
        else:
            sanitized_os_item = strip_ignored_keys(os_item)
            sanitized_scrapy_item = strip_ignored_keys(scrapy_item)
            diff = DeepDiff(
                sanitized_os_item,
                sanitized_scrapy_item,
                ignore_order=True,
                ignore_order_func=diff_ignore_order_callback
            )
            if diff:
                logging.error(f"Differences found for {os_item['identifier']}")
                logging.error(diff.to_json(indent=4))

    logging.info(f"complete! checked {len(os_scrape_data)} items")


if __name__ == "__main__":
    main()
