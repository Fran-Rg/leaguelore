from pathlib import Path
import logging
import scrapy
import requests
from PIL import Image
import sqlite3

import time
import os
import os.path
import math
import re
import shutil
import tempfile
from datetime import datetime, timezone

from scrapy_playwright.page import PageMethod
from scrapy.utils.project import data_path

from translations import LANGS
# "https://universe.leagueoflegends.com/%s/champions/"

DEBUG = os.environ.get("DEBUG", "") != ""

DB_PATH = "lore.db"
DB_REPO = os.environ.get("LORE_DB_REPO", "Fran-Rg/leaguelore")
DB_ASSET_NAME = os.environ.get("LORE_DB_ASSET", DB_PATH)


def _latest_db_asset():
    resp = requests.get(
        "https://api.github.com/repos/%s/releases/latest" % DB_REPO,
        headers={"Accept": "application/vnd.github+json"},
        timeout=30,
    )
    resp.raise_for_status()
    for asset in resp.json().get("assets", []):
        if asset.get("name") == DB_ASSET_NAME:
            updated = datetime.strptime(
                asset["updated_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            return asset["browser_download_url"], updated.timestamp()
    return None, None


def fetch_db():
    """Download lore.db from the latest GitHub release when missing or stale."""
    local_mtime = os.path.getmtime(DB_PATH) if os.path.isfile(DB_PATH) else None
    try:
        url, remote_mtime = _latest_db_asset()
    except requests.RequestException as e:
        logging.warning("Could not check latest '%s' release: %s", DB_REPO, e)
        return
    if url is None:
        logging.warning("No '%s' asset in latest '%s' release", DB_ASSET_NAME, DB_REPO)
        return
    if local_mtime is not None and local_mtime >= remote_mtime:
        logging.info("'%s' is up to date with latest release", DB_PATH)
        return

    logging.info("Downloading '%s' from %s", DB_PATH, url)
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(DB_PATH)), suffix=".part"
        )
        try:
            with os.fdopen(tmp_fd, "wb") as handler:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    handler.write(chunk)
            os.replace(tmp_path, DB_PATH)
        except BaseException:
            os.unlink(tmp_path)
            raise
    # Keep the release timestamp so later runs can compare against it
    os.utime(DB_PATH, (remote_mtime, remote_mtime))
    logging.info("Downloaded '%s' (%s bytes)", DB_PATH, os.path.getsize(DB_PATH))


def download_champ_img(name, image_url):
    safe_img_path = "imgs/%s.jpg" % "".join([c for c in name if re.match(r"\w", c)])
    if not os.path.isfile(safe_img_path):
        img_data = requests.get(image_url).content
        with open(safe_img_path, "wb") as handler:
            handler.write(img_data)
        im_stats = os.stat(safe_img_path)
        x2, y2 = None, None
        while im_stats.st_size > 1024 * 50:  # bigger than 10kb
            im = Image.open(safe_img_path)
            # im = im.convert("L")  # Black & White
            x, y = im.size
            x2, y2 = math.floor(x * 0.9), math.floor(y * 0.9)
            im = im.resize((x2, y2), Image.Resampling.LANCZOS)
            im.save(safe_img_path, optimize=True, quality=95)
            im_stats = os.stat(safe_img_path)
            logging.debug(
                "Reduced '%s' to '%s x %s' : size %s", name, x2, y2, im_stats.st_size
            )
        logging.info("Img '%s' at '%s x %s' : size %s", name, x2, y2, im_stats.st_size)


async def wait_page(response):
    page = response.meta["playwright_page"]
    await page.wait_for_load_state()
    await page.wait_for_timeout(1000)
    await page.close()


class LeagueloreCharacterSpider(scrapy.Spider):
    name = "champions"
    allowed_domains = ["universe.leagueoflegends.com", "yz.lol.qq.com"]
    handle_httpstatus_list = [404]

    def clean(self, s):
        return re.sub(r" *\n +", " ", s.strip()) if s is not None else ""

    def quote_clean(self, s):
        return (
            re.sub(r"^[ “'\"]*(\w.+?)[ '”\"]*$", r"\1", s.strip())
            if s is not None
            else ""
        )

    def drop_cache(self, request):
        fp = self.crawler.request_fingerprinter.fingerprint(request).hex()
        cachedir = data_path(self.settings["HTTPCACHE_DIR"] or "httpcache")
        shutil.rmtree(
            os.path.join(cachedir, self.name, fp[0:2], fp), ignore_errors=True
        )

    def get_champion(self, lang, champion):
        self.cur.execute(
            "select * from champions where champion = ? AND lang = ?",
            (champion, lang),
        )
        return self.cur.fetchone()

    def get_story(self, url):
        self.cur.execute("select * from stories where url = ?", (url,))
        return self.cur.fetchone()

    def build_db(self):
        fetch_db()
        self.con = sqlite3.connect(DB_PATH)
        self.con.row_factory = sqlite3.Row

        ## Create cursor, used to execute commands
        self.cur = self.con.cursor()

        ## Create quotes table if none exists
        self.cur.execute("""
        CREATE TABLE IF NOT EXISTS champions(
            champion TEXT,
            name TEXT,
            lang TEXT,
            bio TEXT,
            race TEXT,
            title TEXT,
            role TEXT,
            region TEXT,
            quote TEXT,
            short_bio TEXT,
            related_champions TEXT,
            PRIMARY KEY (champion, lang)
        )
        """)

        self.cur.execute("""
        CREATE TABLE IF NOT EXISTS stories(
            url TEXT PRIMARY KEY,
            champion TEXT,
            lang TEXT,
            title TEXT,
            content TEXT
        )
        """)

    async def start(self):
        logging.info("Starting")
        self.build_db()
        time.sleep(1)
        for lang in LANGS:
            if DEBUG and lang != "en_US":
                continue  # DEBUG
            yield scrapy.Request(
                "https://yz.lol.qq.com/zh_CN/champions/"
                if lang == "zh_CN"
                else "https://universe.leagueoflegends.com/%s/champions/" % lang,
                cb_kwargs={"lang": lang},
                meta={
                    "playwright": True,
                    "playwright_page_methods": [
                        PageMethod("wait_for_load_state", "domcontentloaded"),
                        PageMethod("wait_for_timeout", 2000),
                    ],
                },
            )

    def parse(self, response, **kwargs):
        logging.info("[%s]Starting Champions Lore Parsing", kwargs["lang"])
        champ_blocks = response.css("li.item_30l8")
        if len(champ_blocks) == 0:
            logging.error(
                "'%s' unable to load champions, skipping: %s",
                kwargs["lang"],
                response.url,
            )
        else:
            for champion in champ_blocks:
                champ_url = champion.css("a")[0].attrib["href"]
                champ_code = champ_url.split("/")[-2]

                cb_kwargs = {"champion": champ_code} | kwargs
                champ_page = response.urljoin(champ_url)
                yield scrapy.Request(
                    champ_page,
                    cb_kwargs=cb_kwargs,
                    callback=self.parse_champion,
                    meta={
                        "playwright": True,
                        "playwright_page_methods": [
                            PageMethod("wait_for_load_state", "domcontentloaded"),
                            PageMethod("wait_for_timeout", 2000),
                        ],
                    },
                )

    def parse_champion(self, response, **kwargs):
        retry_count = kwargs.pop("retry_count", 0)
        # Riot serves valid champion pages with a 404 status, so only trust the in-page marker
        is_404 = response.css("h3.code_Xnqs::text").get() == "404"
        # The region module is sometimes missing when the page hasn't finished hydrating
        is_incomplete = (
            not response.css(".factionText_EnRL h6 span::text").get()
            and not response.css("a.link_3m7v")
        )
        if is_404 or is_incomplete:
            if retry_count >= 5:
                logging.error(
                    "[%s]Gave up after %s retries on incomplete page for '%s': %s",
                    kwargs["lang"],
                    retry_count,
                    kwargs["champion"],
                    response.url,
                )
                return
            logging.warning(
                "[%s]%s for '%s', clearing cache and retrying (%s): %s",
                kwargs["lang"],
                "404" if is_404 else "Incomplete page",
                kwargs["champion"],
                retry_count + 1,
                response.url,
            )
            self.drop_cache(response.request)
            yield scrapy.Request(
                response.url,
                callback=self.parse_champion,
                cb_kwargs=kwargs | {"retry_count": retry_count + 1},
                dont_filter=True,
                meta={
                    "playwright": True,
                    "playwright_page_methods": [
                        PageMethod("wait_for_load_state", "domcontentloaded"),
                        PageMethod("wait_for_timeout", (retry_count + 1) * 1000),
                    ],
                },
            )
            return
        ex_c = self.get_champion(kwargs["lang"], kwargs["champion"])
        if ex_c is None or ex_c["bio"] is None:
            logging.info(
                "[%s]Parsing '%s' : %s", kwargs["lang"], kwargs["champion"], response.url
            )

            role = (
                response.css(".typeDescription_ixWu h6 span::text").get()
                or response.css(".typeDescription_ixWu h6::text").get()
            )
            race = (
                response.css(".race_3k58 h6 span::text").get()
                or response.css(".race_3k58 h6::text").get()
            )
            short_bio = (
                response.css(".biographyText_3-to p::text").get()
                or response.css(".biographyText_3-to::text").get()
                or response.css(".biographyText_3-to p i::text").get()
            )

            name = response.css("title::text")[0].get().split(" - ")[0]
            title = response.css("h3.subheadline_rlsJ::text").get()
            quote = (
                response.css("li.quote_2507 p::text").get()
                or response.css("li.quote_2507 p i::text").get()
            )
            region = (
                response.css(".factionText_EnRL h6 span::text").get()
                or response.css("a.link_3m7v")[0].attrib["href"].split("/")[-2].title()
            )
            champ_parse = {
                "name": name,
                "race": race,
                "title": title,
                "role": role,
                "region": region,
                "quote": self.quote_clean(quote),
                "short_bio": self.clean(short_bio),
                "related_champions": ",".join(
                    [
                        i.css("a h5::text").get()
                        for i in response.css("ul.champions_jmhN li")
                    ]
                ),
            } | kwargs
            logging.info(
                "[%s]Cur champ parse: '%s[%s]'", kwargs["lang"], kwargs["champion"], name
            )
            bio_url = next(
                (
                    i.attrib.get("href")
                    for i in response.css("a")
                    if i.attrib.get("href", "").startswith("/%s/story/" % kwargs["lang"])
                ),
                None,
            )
            if bio_url is None:
                bio_url = "/%s/story/champion/%s/" % (kwargs["lang"], name.lower())
                logging.error(
                    "[%s]first bio url is null for: '%s[%s]'",
                    kwargs["lang"],
                    kwargs["champion"],
                    champ_parse["name"],
                )
            # logging.info("[%s]Bio URL %s",response.url, bio_url)
            if bio_url is not None:
                bio_page = response.urljoin(bio_url)
                # logging.error("bio_page %s", bio_page)
                request = scrapy.Request(
                    bio_page,
                    callback=self.parse_bio,
                    cb_kwargs=champ_parse,
                    meta={
                        "playwright": True,
                        "playwright_page_methods": [
                            PageMethod("wait_for_load_state", "domcontentloaded"),
                            PageMethod("wait_for_timeout", 1000),
                        ],
                    },
                )
                yield request
            else:
                logging.error("No bio for '%s': %s", name, response.url)

        story_urls = response.xpath(
            '//span[@data-gettext-identifier="module-story-cta"]/ancestor::a[1]/@href'
        ).getall()
        for story_url in story_urls:
            story_page = response.urljoin(story_url)
            if self.get_story(story_page) is not None:
                logging.debug("[%s]Story already in database: %s", kwargs["lang"], story_page)
                continue
            yield scrapy.Request(
                story_page,
                callback=self.parse_story,
                cb_kwargs={"champion": kwargs["champion"], "lang": kwargs["lang"]},
                meta={
                    "playwright": True,
                    "playwright_page_methods": [
                        PageMethod("wait_for_load_state", "domcontentloaded"),
                        PageMethod("wait_for_timeout", 1000),
                    ],
                },
            )

    def parse_bio(self, response, **kwargs):
        # bio = "".join(i.get() for i in response.xpath('//*[@id="CatchElement"]/*'))
        bio = response.css(".root_3nvd.dark_1RHo").get()
        if bio is None:
            logging.error(
                "[%s]No bio content for '%s', clearing cache: %s",
                kwargs["lang"],
                kwargs["champion"],
                response.url,
            )
            self.drop_cache(response.request)
            return

        image_url = response.css("div.image_3oOd.backgroundImage_5wQJ")[0].attrib[
            "data-am-url"
        ]
        download_champ_img(kwargs["champion"], image_url)

        champ_parse = {"bio": bio} | kwargs
        self.save_champ(champ_parse)
        yield champ_parse

        story_links = response.css("a.root_K4Th")
        if len(story_links) == 0:
            logging.warning(
                "[%s]No Story from bio for '%s[%s]'",
                kwargs["lang"],
                kwargs["champion"],
                response.url,
            )
            return

        for link in story_links:
            story_url = link.attrib["href"]
            if self.get_story(story_url) is not None:
                logging.debug(
                    "[%s]Story already in database: %s", kwargs["lang"], story_url
                )
                continue
            yield scrapy.Request(
                response.urljoin(story_url),
                callback=self.parse_story,
                cb_kwargs={"champion": kwargs["champion"], "lang": kwargs["lang"]},
                meta={
                    "playwright": True,
                    "playwright_page_methods": [
                        PageMethod("wait_for_load_state", "domcontentloaded"),
                        PageMethod("wait_for_timeout", 1000),
                    ],
                },
            )

    def parse_story(self, response, **kwargs):
        retry_count = kwargs.pop("retry_count", 0)
        content = response.css(".root_3nvd.dark_1RHo").get()
        if content is None:
            if retry_count >= 5:
                logging.error(
                    "[%s]Gave up after %s retries on empty story: %s",
                    kwargs["lang"],
                    retry_count,
                    response.url,
                )
                return
            logging.warning(
                "[%s]No story content, clearing cache and retrying (%s): %s",
                kwargs["lang"],
                retry_count + 1,
                response.url,
            )
            self.drop_cache(response.request)
            yield scrapy.Request(
                response.url,
                callback=self.parse_story,
                cb_kwargs=kwargs | {"retry_count": retry_count + 1},
                dont_filter=True,
                meta={
                    "playwright": True,
                    "playwright_page_methods": [
                        PageMethod("wait_for_load_state", "domcontentloaded"),
                        PageMethod("wait_for_timeout", 1000),
                    ],
                },
            )
            return

        story = {
            "url": response.url,
            "champion": kwargs["champion"],
            "lang": kwargs["lang"],
            "title": self.clean(
                response.css("div.noHeaderTitle_1n0i::text").get()
                or response.css("h1.title_121J::text").get()
            ),
            "content": content,
        }
        self.save_story(story)
        yield story

    def save_champ(self, c):
        logging.info("[%s]Saving %s to DB", c["lang"], c["champion"])
        self.cur.execute(
            """
                INSERT OR REPLACE INTO champions(
            champion,
            name,
            lang,
            bio,
            race,
            title,
            role,
            region,
            quote,
            short_bio,
            related_champions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c["champion"],
                c["name"],
                c["lang"],
                c["bio"],
                c["race"],
                c["title"],
                c["role"],
                c["region"],
                c["quote"],
                c["short_bio"],
                c["related_champions"],
            ),
        )
        self.con.commit()

    def save_story(self, s):
        logging.info(
            "Saving story '%s' for %s to DB -> %s", s["title"], s["champion"], s["url"]
        )
        self.cur.execute(
            """
            INSERT OR REPLACE INTO stories(url, champion, lang, title, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            (s["url"], s["champion"], s["lang"], s["title"], s["content"]),
        )
        self.con.commit()
