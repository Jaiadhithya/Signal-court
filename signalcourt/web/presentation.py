"""Present research without rating badges; the source report and ledger remain intact."""

from lxml import html


def research_report(source: str) -> str:
    document = html.document_fromstring(source)
    selectors = [
        '//*[@id="strength"]',
        '//a[@href="#strength"]',
        '//*[contains(concat(" ",normalize-space(@class)," ")," grade-box ")]',
        '//span[contains(concat(" ",normalize-space(@class)," ")," tag ") and '
        '(contains(concat(" ",@class," ")," weak ") or contains(concat(" ",@class," ")," moderate ") or '
        'contains(concat(" ",@class," ")," strong ") or contains(concat(" ",@class," ")," killed "))]',
        '//tr[.//span[@class="record-type" and normalize-space()="VERDICT"]]',
        '//div[@class="count-grid"]/p[span[normalize-space()="VERDICT"]]',
        '//article[contains(concat(" ",@class," ")," rejected ")][.//h3[contains(.,"Killed hypothesis")]]',
    ]
    for selector in selectors:
        for element in document.xpath(selector):
            element.drop_tree()
    for link in document.xpath('//a[starts-with(@href,"#VD-")]'):
        link.drop_tag()
    return html.tostring(document, encoding="unicode", doctype="<!doctype html>")
