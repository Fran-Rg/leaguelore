from ebooklib import epub
from PIL import Image
import sqlite3

import csv
import io
from itertools import groupby
from operator import itemgetter
import json
import re

from translations import TRANSLATIONS, LANGS


def get_safe_img_path(name):
    return "imgs/%s.jpg" % "".join([c for c in name if re.match(r"\w", c)])


def write_book(data, lang):
    print("Lang '%s' - Building Book" % lang)
    book = epub.EpubBook()

    book.set_cover("cover.jpg", open("imgs/leagueoflegendslogo.jpg", "rb").read())
    # set metadata
    # book.set_identifier("lollore1")
    book.set_title("League of Legends Lore - %s" % lang)
    book.set_language(lang)

    book.add_author("Riot Games")
    book.add_author(
        "Francois Regnoult",
        uid="scripter",
    )
    data.sort(key=itemgetter("region"))

    # Build the navigation in advance for reference
    i = 1
    champ_chap = {}
    for region, champs in groupby(data, itemgetter("region")):
        champs = list(champs)
        champs.sort(key=itemgetter("name"))
        for c in champs:
            champ_chap[c["name"]] = str(i).zfill(3)
            i += 1
    champ_count = len(data)

    # Build all chapters
    allchapters = []
    allregionschapters = tuple()
    for region, champs in groupby(data, itemgetter("region")):
        regionchapters = []
        champs = list(champs)
        champs.sort(key=itemgetter("name"))
        for d in champs:
            c = epub.EpubHtml(
                title=d["name"], file_name="chap_%s.xhtml" % champ_chap[d["name"]]
            )
            # print("Lang '%s' - Champion '%s'" % (lang, d["name"]))
            related = [
                '<li><a href="chap_%s.xhtml">%s</a></li>'
                % (champ_chap.get(i, "000"), i)
                for i in d["related_champions"].split(",")
                if i != ""
            ]
            champ_img_path = get_safe_img_path(d["champion"])
            # load Image file
            img1 = Image.open(champ_img_path)
            b = io.BytesIO()
            img1.save(b, "jpeg")
            b_imagechamp = b.getvalue()
            image1_item = epub.EpubItem(
                uid=d["name"],
                file_name=champ_img_path,
                media_type="image/jpeg",
                content=b_imagechamp,
            )
            book.add_item(image1_item)

            content = [
                '<img style="max-width: 100%%; width: 100%%;" alt="%s" src="%s"/>'
                % (d["name"], champ_img_path),
                "<h1>%s</h1>" % d["name"],
                "<h2><i>%s</i></h2>" % d["title"],
                "<h3>%s: %s</h3>" % (TRANSLATIONS[lang]["region"], d["region"]),
                "<h3><i>%s</i></h3>" % d["quote"]
                if d["quote"].startswith("«")
                else "'" + d["quote"] + "'",
            ]
            if len(related) > 0:
                content += [
                    "<h3>%s: <ul>%s<ul></h3>"
                    % (TRANSLATIONS[lang]["related"], "".join(related))
                ]
            content += [
                "<p>%s</p>" % d["short_bio"],
                "<h2>%s</h2>" % TRANSLATIONS[lang]["bio"],
                "<p>%s</p>" % d["bio"],
            ]
            if d["story"] != "":
                content.append("<h2>%s</h2>" % TRANSLATIONS[lang]["story"])
                content.append("<p>%s</p>" % d["story"])
            c.content = "".join(content)

            # add chapter
            book.add_item(c)
            regionchapters.append(c)
        allchapters += regionchapters
        allregionschapters += (
            (
                epub.Section(region),
                tuple(regionchapters),
            ),
        )

    # Table of content
    book.toc = allregionschapters
    # add default NCX and Nav file
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # define CSS style
    style = "BODY {color: white;}"
    nav_css = epub.EpubItem(
        uid="style_nav",
        file_name="style/nav.css",
        media_type="text/css",
        content=style,
    )

    # add CSS file
    book.add_item(nav_css)

    # basic spine
    book.spine = [
        "nav",
    ] + allchapters

    setattr(book.get_item_with_id("cover"), "is_linear", True)

    # write to the file
    book_file_name = "league_of_legends_%s_%s.epub" % (lang, champ_count)
    epub.write_epub(book_file_name, book, {})
    print("Lang '%s' - Book '%s' done" % (lang, book_file_name))

    return book_file_name


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def load():
    data = []
    con = sqlite3.connect("lore.db")
    con.row_factory = dict_factory

    ## Create cursor, used to execute commands
    cur = con.cursor()
    cur.execute("select * from champions;")
    data = cur.fetchall()
    data.sort(key=itemgetter("lang"))
    countries_data = {
        region: [c for c in cdata]
        for region, cdata in groupby(data, itemgetter("lang"))
        if region in LANGS
    }
    return countries_data


def run():
    data = load()
    meta = {
        "tot_champ_count": 0,
        "champ_count": 0,
        "langs": sorted(data.keys()),
        "allchamps": True,
        "books": [],
    }
    print("Langs count: %s" % len(data))
    for lang, d in data.items():
        c_count = len(d)
        print("Lang: %s - Count: %s" % (lang, c_count))
        meta["tot_champ_count"] += c_count
        if meta["champ_count"] > 0:
            meta["allchamps"] = meta["allchamps"] and (c_count == meta["champ_count"])
        meta["champ_count"] = max(c_count, meta["champ_count"])
        meta["books"].append(write_book(d, lang))
    print(meta)
    with open("meta.json", "w") as f:
        json.dump(meta, f)


if __name__ == "__main__":
    run()
