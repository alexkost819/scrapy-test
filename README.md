# Scrapy PoC for Plural Open Scrapers

Proof of Concept for using Scrapy to write and execute scrapers that obtain open civic data.

## Try it

* Install the necessary version of python using `pyenv`
* If necessary, `pip install poetry`
* `poetry install`: You should now have the `scrapy` tool installed in your poetry environment.
* Scrapy commands should now work via `poetry run scrapy`

### Run a scout scrape with local single JSON file output

`poetry run scrapy crawl nv-bills -a session=2023Special35 -O nv-bills-scout.json -s "ITEM_PIPELINES={}"`

* This command disables the `DropStubsPipeline` (`ITEM_PIPELINES={}`), which by default drops stub entities
* Results are output to `nv-bills-scout.json`

### Run a scout scrape with local JSON file-per-bill-stub output

`poetry run scrapy crawl nv-bills -a session=2023Special35 -O nv-bills-scout.json -s "ITEM_PIPELINES={\"scrapers.pipelines.SaveLocalPipeline\": 300}"`

* Scrapers can define when to `yield BillStubItem(BillStub)` when the scraper has enough basic data about a bill
* Requests must be provided the `meta` keyword parameter with a value of `{"is_scout": True}`, or else they will be
  dropped by the `ScoutOnlyDownloaderMiddleware`
    * In most cases this allows the scraper to skip many subsidiary requests, speeding up the scrape time
* This command disables the `DropStubsPipeline` (excluded from `ITEM_PIPELINES`), which by default drops stub entities
* Results are output to individual JSON files in a folder like `_data/{jurisdiction}/{date}/{runNumber}`

### Run a full scrape with local JSON file-per-entity output

`poetry run scrapy crawl nv-bills -a session=2023Special35 -s "DOWNLOADER_MIDDLEWARES={}" -s "ITEM_PIPELINES={\"scrapers.pipelines.DropStubsPipeline\": 300, \"scrapers.pipelines.SaveLocalPipeline\": 301}"`

* This command enables the `SaveLocalPipeline` which saves JSON files to a local folder in the style of Open States
  scrapers
* This command disables the `ScoutOnlyDownloaderMiddleware` (`DOWNLOADER_MIDDLEWARES={}`), so all requests will be made.
* However `DropStubsPipeline` is enabled, so stub bills will not be emitted
* Results are output to individual JSON files in a folder like `_data/{jurisdiction}/{date}/{runNumber}`

### Run a full scrape with Google Cloud Storage JSON file-per-entity output

`poetry run scrapy crawl nv-bills -a session=2023Special35 -s "DOWNLOADER_MIDDLEWARES={}" -s "ITEM_PIPELINES={\"scrapers.pipelines.DropStubsPipeline\": 300, \"scrapers.pipelines.SaveGoogleCloudStoragePipeline\": 301}"`

* This command enables the `SaveGoogleCloudStoragePipeline` which saves JSON files to a Google Cloud Storage location
  in the style of Open States scrapers.
* Scrapy settings (ie `settings.py`) must include the following to tell the pipeline which bucket and prefix to store
  to:

```python
SAVE_GOOGLE_CLOUD_STORAGE = {
    "bucket": "plural-dev-lake-raw",
    "prefix": "legislation",
}
```

* You must have local google credentials that
  work ([see GCS python client docs](https://cloud.google.com/python/docs/reference/storage/latest))
* This command disables the `ScoutOnlyDownloaderMiddleware` (`DOWNLOADER_MIDDLEWARES={}`), so all requests will be made.
* However `DropStubsPipeline` is enabled, so stub bills will not be emitted
* Results are output to individual JSON files to two locations in Cloud Storage:
    * General jurisdiction
      entities: `{prefix}/country:{country_code}/state:{jurisdiction_abbreviation}/{ISO 8601 date of scraper start}/`
    * Legislative session entities (eg
      bills):
      `{prefix}/country:{country_code}/state:{jurisdiction_abbreviation}/legislative_sessions/{session_identifier}/{ISO 8601 date of scraper start}/`

### Run a full scrape AND save source files with Google Cloud Storage JSON file-per-entity output

`poetry run scrapy crawl nv-bills -a session=2023Special35 -s "DOWNLOADER_MIDDLEWARES={}" -s "ITEM_PIPELINES={\"scrapers.pipelines.DropStubsPipeline\": 300, \"scrapy.pipelines.files.FilesPipeline\": 301, \"scrapers.pipelines.SaveGoogleCloudStoragePipeline\": 302}"`

* All of the above ("Run a full scrape with Google Cloud Storage...") instructions/notes apply here!
* In addition, you need to have the following in Scrapy `settings.py`:

```python
FILES_STORE = "gs://plural-dev-lake-raw/legislation/source-files/"
GCS_PROJECT_ID = "civic-eagle-enview-dev"
```

* This uses the built-in Files pipeline to save original files to a GCS
  location (`plural-dev-lake-raw/legislation/source-files/full` per setting above). Each file is named by generating
  a SHA1 hash of the URL where the original file was found. So program code can use a snippet (see below) to take a
  bill version file URL and identify the corresponding file in Google Cloud Storage.
* At the time of writing, only `BillItem` implements file downloading, and only for bill version documents. However
  additional documents can be easily added by simply adding URLs to the `file_urls` field on `BillItem` (or other
  items).

### Generate the hashcode representing a file from its original URL

The Scrapy Files pipeline, by default, saves files using a filename generated from a hash of its original URL. This is
cleaner than trying to normalize URLs to strings for a filesystem, but is a bit opaque. You can use this code snippet
to identify a filename that corresponds to the source URL of the file:

```bash
jesse@greenbookwork:~/repo/openstates/scrapy-test$ poetry run python
Python 3.9.15 (main, Jul 25 2023, 18:39:01)
[GCC 11.3.0] on linux
Type "help", "copyright", "credits" or "license" for more information.
>>> import hashlib
>>> from pathlib import Path
>>> from scrapy.utils.python import to_bytes
>>> url = "https://www.leg.state.nv.us/Session/35th2023Special/Bills/SR/SR1.pdf"
>>> hashlib.sha1(to_bytes(url)).hexdigest() + Path(url).suffix
'7952f76537995a8c615afcc3848d60b44a52c490.pdf'
```

### Quickly examine individual items in scrapy output

The CLI tool `jq` is very useful for quickly examining output, especially when you have a full scrape that is a long
JSON file. For example, this command finds a particular Bill and outputs the `subject` property of that bill:

`cat nv-bills-82-2.json | jq -c '.[] | select(.identifier | contains("AB261"))' | jq .subject`

### Run scrapy shell

The `scrapy shell` command is useful for interacting with a particular webpage and experimenting with selectors/parsing.
Note that you should run this with the scout-only downloader middlware turned off or you will get silent failures to
fetch that will be confusing:

`poetry run scrapy shell -s "DOWNLOADER_MIDDLEWARES={}" https://google.com`

## Context

### Prior art

John did a scrapy PoC in the (
private) [Plural Engineering Experiments repo](https://github.com/civic-eagle/data-engineering-experiements/tree/main/scrapy-test)

### Why Scrapy?

* Very popular: easy to find developers who are familiar
* Very mature: battle-tested layers of abstraction, flexibility to meet our goals
* Reduce overall surface area of "in-house" code we need to maintain

### Criteria

One way to think of success for this project: can it achieve most or all of the goals of the spatula project, without
requiring much custom code?

* Can we run a scout scrape that returns basic info on entities without making the extra requests required for
  retrieving full info on entities?
* Can we run a normal scrape that does not output the partial/stub info generated by scout code?
* Are there barriers involved in using necessary elements of `openstates-core` code here? For instance, we want to be
  able to easily port code, and continue to use the core entity models w/ helper functions etc.
* Can the scraper output an equivalent directory of json files that can be compared 1:1 to an existing scraper?
* Can we emit output in a "near-realtime" manner, so that we can forward data from the scraper to a data cloud or
  other processing without waiting for the entire scrape to complete?
* Can we easily control the retry/backoff/SSL verification/HTTP response handling on a per-jurisdiction/domain basis,
  without having to modify multiple parts of scraper logic?
* Can we write scrapers so that they are resumable? in other words, can a scrape that fails at 6 hours in be then
  restarted near the point where it failed (either by using an HTTP cache or not-too-complicated pagination)?

## Technical notes

### Porting existing Open States scrapers

We have a repository of existing open data scrapers
in [openstates-scrapers](https://github.com/openstates/openstates-scrapers).
These form the baseline of quality and expected output for scrapers in this test repository.

Those scrapers rely on some shared code from a PyPi package called `openstates`, the code for
which [is found here](https://github.com/openstates/openstates-core).
There is currently a barrier to adding that shared code to this repo (see Problems below), so some of that shared code
is temporarily copied into this repo.

Some technical notes regarding porting code:

* All the scrapers in the `scrapers_next` folder use the [spatula](https://github.com/jamesturk/spatula) scraper
  framework. A few of the ones in the `scrapers` folder do as well (see `nv/bills.py`). But most of the scrapers in the
  `scrapers` folder use an older framework called [scrapelib](https://github.com/jamesturk/scrapelib).
* There are often multiple requests needed to compile enough data to fully represent a Bill, so these sequences of
  requests and parsing can end up looking like long procedures (scrapelib) or nested abstractions where it's not clear
  how they are tied together (spatula). In scrapy, we should handle this by yielding Requests that pass along the
  partial entity using `cb_kwargs`.
    * In spatula, you'll see a pattern where subsequent parsing functions access `self.input` to access that partial
      data. In scrapy, passed-down partial data is available as a named kwarg, such as `bill` or `bill_stub`.
* Fundamentally, the CSS and Xpath selectors remain the same, just some of the syntax around them changes:
    * `doc.xpath()` or `self.root.xpath()` becomes `response.xpath()`
    * `CSS("#title").match_one(self.root).text` becomes ` response.css("#title::text").get()`

#### Evaluating whether the port is a success

The scrapy-based scrapers need to perform at least as well as the equivalent scraper in that repo.

The most important expectation to meet is that the new scraper must be at least as information-complete and accurate as
the old scraper. Is the output the same (or better)?
See [documentation on Open States scrapers](https://docs.openstates.org/contributing/scrapers/).

Old Open States scrapers will output a JSON file for each scraped item to a local directory: `./_data`:

```shell
jesse@greenbookwork:~/repo/openstates/openstates-scrapers/scrapers/_data$ cd nv
jesse@greenbookwork:~/repo/openstates/openstates-scrapers/scrapers/_data/nv$ ls -alh
total 56K
drwxrwxr-x 2 jesse jesse 4.0K Nov  6 18:29 .
drwxrwxr-x 6 jesse jesse 4.0K Nov  5 19:36 ..
-rw-rw-r-- 1 jesse jesse 2.1K Nov  6 18:28 bill_9f6f717c-7d04-11ee-aeef-01ae5adc5576.json
-rw-rw-r-- 1 jesse jesse 2.1K Nov  6 18:28 bill_a2526bec-7d04-11ee-aeef-01ae5adc5576.json
-rw-rw-r-- 1 jesse jesse  13K Nov  6 18:29 bill_a54dad84-7d04-11ee-aeef-01ae5adc5576.json
-rw-rw-r-- 1 jesse jesse 1.9K Nov  6 18:29 bill_a8d58f30-7d04-11ee-aeef-01ae5adc5576.json
-rw-rw-r-- 1 jesse jesse 2.1K Nov  6 18:29 bill_abc9288c-7d04-11ee-aeef-01ae5adc5576.json
-rw-rw-r-- 1 jesse jesse 3.8K Nov  6 18:28 jurisdiction_ocd-jurisdiction-country:us-state:nv-government.json
-rw-rw-r-- 1 jesse jesse  171 Nov  6 18:28 organization_9dac2f10-7d04-11ee-aeef-01ae5adc5576.json
-rw-rw-r-- 1 jesse jesse  187 Nov  6 18:28 organization_9dac2f11-7d04-11ee-aeef-01ae5adc5576.json
-rw-rw-r-- 1 jesse jesse  189 Nov  6 18:28 organization_9dac2f12-7d04-11ee-aeef-01ae5adc5576.json
```

The above output was created by running the following command from within the `scrapers` subdirectory:

`poetry run python -m openstates.cli.update --scrape nv bills session=2023Special35`

(Nevada session `2023Special35` is a nice example because it is quick: only 5 bills.)

We can compare this output to the `nv-bills.json` output mentioned above.

Other evaluation criteria:

* A scraper for bills should accept a `session` argument that accepts a legislative session identifier (string).
  [See example](https://github.com/openstates/scrapy-test/blob/30dc3188429ecb015c1144dc16466afd4032bd63/scrapers/scrapers/spiders/nv-bills.py#L92).
* Comments that provide context for otherwise-opaque HTML selectors/traversal are helpful!
* Long procedures should be broken into reasonable-sized functions. Often it makes sense to have a separate function for
  handling sub-entities, eg `add_actions()`, `add_sponsors()`, `add_versions()` etc..
* `Request`s that are required to get the `BillStub` level of info should have `"is_scout": True` set in the `meta` arg.
  This allows us to run the scraper in "scout" mode: only running the minimum requests needed for basic info so we can
  frequently assess when new entities are posted (and avoid flodding the source with requests).

### Problems

* The `spatula` library pins `attrs<21`, which conflicts with `scrapy` (via Twisted). `openstates` now makes
  `spatula` an optional extra, so this project depends on `openstates` directly (currently as a local path
  dependency on `../openstates-core`). Don't install `openstates[spatula]` here.
* Scraper is not fully ported

### Useful concepts

* Pass input properties from the comamnd line to the scraper using the `-a` flag, ie `-a session=2023Special35`. This
  allows us to copy `os-update` behavior where we can pass in runtime parameters to the scraper.
* Override scrapy settings at runtime with the `-s` flag. This allows us to set things like which Item pipelines and
  middleware is enabled at runtime. This allows us to switch the behavior between scout/normal scrape at runtime.
* Item pipelines do things with items returned by scrapers. Using this to drop "stub" items when in normal scrape mode.
* Downloader middleware allows us to change behavior of a Request before it is made. Currently requiring the scraper to
  mark `is_scout: True` on the `meta` property of the Request, so that we can ignore non-scout requests when desired.