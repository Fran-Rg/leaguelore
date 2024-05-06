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

from scrapy_playwright.page import PageMethod

from translations import LANGS
# "https://universe.leagueoflegends.com/%s/champions/"

PREVIOUS_CHAMP_COUNT = int(os.environ.get("PREVIOUS_CHAMP_COUNT", "0"))


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
                "Reduced '%s' to '%s x %s' : size %s" % (name, x2, y2, im_stats.st_size)
            )
        print("Img '%s' at '%s x %s' : size %s" % (name, x2, y2, im_stats.st_size))


async def wait_page(response):
    page = response.meta["playwright_page"]
    await page.wait_for_load_state()
    await page.wait_for_timeout(1000)
    await page.close()


class LeagueloreCharacterSpider(scrapy.Spider):
    name = "champions"
    allowed_domains = ["universe.leagueoflegends.com", "yz.lol.qq.com"]

    def clean(self, s):
        return re.sub(r" *\n +", " ", s.strip()) if s is not None else ""

    def quote_clean(self, s):
        return (
            re.sub(r"^[ “'\"]*(\w.+?)[ '”\"]*$", r"\1", s.strip())
            if s is not None
            else ""
        )

    def build_db(self):
        self.con = sqlite3.connect("lore.db")

        ## Create cursor, used to execute commands
        self.cur = self.con.cursor()

        ## Create quotes table if none exists
        self.cur.execute("""
        CREATE TABLE IF NOT EXISTS champions(
            champion TEXT,
            name TEXT,
            lang TEXT,
            story TEXT,
            bio TEXT,
            race TEXT,
            title TEXT,
            role TEXT,
            region TEXT,
            quote TEXT,
            short_bio TEXT,
            related_champions TEXT
        )
        """)

    def start_requests(self):
        print("Starting")
        self.build_db()
        for lang in LANGS:
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
            time.sleep(1)

    def parse(self, response, **kwargs):
        print("Starting Champions '%s'" % kwargs["lang"])
        champ_blocks = response.css("li.item_30l8")
        print(
            "Found new champs (%s)? %s"
            % (len(champ_blocks), len(champ_blocks) > PREVIOUS_CHAMP_COUNT)
        )
        if len(champ_blocks) > PREVIOUS_CHAMP_COUNT:
            for champion in champ_blocks:
                champ_url = champion.css("a")[0].attrib["href"]
                champ_code = champ_url.split("/")[-2]
                self.cur.execute(
                    "select * from champions where champion = ? AND lang = ?",
                    (champ_code, kwargs["lang"]),
                )
                result = self.cur.fetchone()

                # If it is in DB, create log message
                if result:
                    pass
                    # print(
                    #     "[%s]%s already in database"
                    #     % (
                    #         kwargs["lang"],
                    #         champ_code,
                    #     )
                    # )
                else:
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
        elif len(champ_blocks) == 0:
            logging.error(
                "'%s' unable to load champions, skipping: %s",
                kwargs["lang"],
                response.url,
            )
        else:
            logging.error("No new champs found stopping...")
            self.crawler.engine.close_spider(self, "No new champs found")

    def parse_champion(self, response, **kwargs):
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
        title = response.css("h3.subheadline_rlsJ::text")[0].get()
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
        print(
            "'%s' Cur champ parse: '%s[%s]'"
            % (kwargs["lang"], kwargs["champion"], name)
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
            logging.error("first bio url is null for: %s", champ_parse)
        # print("[%s]Bio URL %s" % (response.url, bio_url))
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

    def parse_bio(self, response, **kwargs):
        # bio = "".join(i.get() for i in response.xpath('//*[@id="CatchElement"]/*'))
        bio = response.css(".root_3nvd.dark_1RHo").get()

        image_url = response.css("div.image_3oOd.backgroundImage_5wQJ")[0].attrib[
            "data-am-url"
        ]
        download_champ_img(kwargs["champion"], image_url)

        champ_parse = {"bio": bio} | kwargs
        story_link = response.css("a.root_K4Th")
        # print("[%s]story_link %s" % (response.url, story_link))
        if len(response.css("a.root_K4Th")) > 0:
            story_url = story_link[0].attrib["href"]
            request = scrapy.Request(
                response.urljoin(story_url),
                callback=self.parse_story,
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
            logging.error("No Story for '%s'", kwargs["name"])
            champ_parse = {"story": ""} | champ_parse
            self.save_champ(champ_parse)
            yield champ_parse

    def parse_story(self, response, **kwargs):
        story = response.css(".root_3nvd.dark_1RHo").get()
        champ_parse = {"story": story} | kwargs
        self.save_champ(champ_parse)
        yield champ_parse

    def save_champ(self, c):
        print("Saving [%s]%s to DB" % (c["lang"], c["champion"]))
        self.cur.execute(
            """
                INSERT INTO champions(
            champion,
            name,
            lang,
            story,
            bio,
            race,
            title,
            role,
            region,
            quote,
            short_bio,
            related_champions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c["champion"],
                c["name"],
                c["lang"],
                c["story"],
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
