# -*- coding: utf-8 -*-
#
# This file is part of hepcrawl.
# Copyright (C) 2016, 2017 CERN.
#
# hepcrawl is a free software; you can redistribute it and/or modify it
# under the terms of the Revised BSD License; see LICENSE file for
# more details.

"""Spider for POS."""

from __future__ import absolute_import, division, print_function

import re

from urlparse import urljoin

from scrapy import Request, Selector

from . import StatefulSpider
from ..dateutils import create_valid_date
from ..items import HEPRecord
from ..loaders import HEPLoader
from ..utils import (
    get_licenses,
    get_first,
    ParsedItem,
)


DEFAULT_BASE_URL = 'https://pos.sissa.it'
DEFAULT_BASE_CONFERENCE_PAPER_URL = (
    DEFAULT_BASE_URL + '/contribution?id='
)
DEFAULT_BASE_PROCEEDINGS_URL = (
    DEFAULT_BASE_URL + '/cgi-bin/reader/conf.cgi?confid='
)


class PoSExtractionException(Exception):
    pass


class POSSpider(StatefulSpider):
    """POS/Sissa crawler.

    From PoS we create two types of records, a conference paper record, and a
    conference proceedings record.

    The bulk of the records comes from oaiharvest, and this spider crawls the
    files generated by it.

    For the conference paper record we have to scrape also the html page of the
    record on the PoS website to get the pdf link. (see
    `DEFAULT_BASE_CONFERENCE_PAPER_URL`)

    Then, from that same page, we get the internal conference id.

    With that conference id, then we scrape the conference proceedings page,
    and extract the information to create the proceedings record. (see
    `DEFAULT_BASE_PROCEEDINGS_URL`)

    To do that and because each needs the information of the previous, the
    spider must use the callbacks system provided by scrapy through the
    :ref:`scrapy.html.response.Response` callback parameter, and chain the
    parser functions.

    The deduplication of the conference proceedings papers is left for the
    `HepcrawlCrawlOnceMiddleware` middleware.

    Example:
        ::
            $ scrapy crawl PoS \\
                -a "source_file=file://$PWD/tests/unit/responses/pos/sample_pos_record.xml"
    """
    name = 'pos'

    def __init__(
        self,
        source_file=None,
        base_conference_paper_url=DEFAULT_BASE_CONFERENCE_PAPER_URL,
        base_proceedings_url=DEFAULT_BASE_PROCEEDINGS_URL,
        **kwargs
    ):
        super(POSSpider, self).__init__(**kwargs)
        self.source_file = source_file
        self.BASE_CONFERENCE_PAPER_URL = base_conference_paper_url
        self.BASE_PROCEEDINGS_URL = base_proceedings_url

    def start_requests(self):
        yield Request(self.source_file)

    def parse(self, response):
        self.log('Got record from: {response.url}'.format(**vars()))

        response.selector.remove_namespaces()
        record_xml_selectors = response.selector.xpath('.//record')
        for record_xml_selector in record_xml_selectors:
            yield self.get_conference_paper_page_request(
                xml_selector=record_xml_selector,
            )

    def get_conference_paper_page_request(self, xml_selector, meta=None):
        """Gets the conference paper html page, for the pdf link for the
        conference paper, and later the internal conference id.
        """
        meta = meta or {}

        identifier = xml_selector.xpath(
            './/metadata/pex-dc/identifier/text()'
        ).extract_first()
        conference_paper_url = "{0}{1}".format(
            self.base_conference_paper_url,
            identifier,
        )
        meta['xml_record'] = xml_selector.extract()

        # the meta parameter will be passed over to the callback as a property
        # in the response parameter
        return Request(
            url=conference_paper_url,
            callback=self.parse_conference_paper,
            meta=meta
        )

    def parse_conference_paper(self, response):
        xml_record = response.meta.get('xml_record')
        conference_paper_url = response.url
        conference_paper_pdf_url = self._get_conference_paper_pdf_url(
            conference_paper_page_html=response.body,
        )

        parsed_conference_paper = self.build_conference_paper_item(
            xml_record=xml_record,
            conference_paper_url=conference_paper_url,
            conference_paper_pdf_url=conference_paper_pdf_url,
        )
        yield parsed_conference_paper

        # prepare next callback step
        response.meta['html_record'] = response.body
        yield self.get_conference_proceedings_page_request(
            meta=response.meta,
        )

    def get_conference_proceedings_page_request(self, meta):
        """Gets the conference proceedings page, using the indernal conference
        id from the record html page retrieved before.
        """
        if not meta.get('html_record'):
            raise PoSExtractionException(
                'PoS conference paper page was empty, current meta:\n%s' % meta
            )

        proceedings_page_url = self._get_proceedings_page_url(
            page_html=meta.get('html_record'),
        )

        page_selector = Selector(
            text=meta.get('xml_record'),
            type='xml',
        )
        page_selector.remove_namespaces()
        pos_id = page_selector.xpath(
            ".//metadata/pex-dc/identifier/text()"
        ).extract_first()
        meta['pos_id'] = pos_id

        return Request(
            url=proceedings_page_url,
            meta=meta,
            callback=self.parse_conference_proceedings,
        )

    def parse_conference_proceedings(self, request):
        parsed_conference_proceedings = self.build_conference_proceedings_item(
            proceedings_page_html=request.body,
            pos_id=request.meta['pos_id'],
        )
        yield parsed_conference_proceedings

    def _get_proceedings_page_url(self, page_html):
        page_selector = Selector(
            text=page_html,
            type="html"
        )
        internal_url = page_selector.xpath(
            "//a[not(contains(text(),'pdf'))]/@href",
        ).extract_first()
        proceedings_internal_id = internal_url.split('/')[1]
        return '{0}{1}'.format(
            self.base_proceedings_url,
            proceedings_internal_id,
        )

    def build_conference_paper_item(
        self,
        xml_record,
        conference_paper_url,
        conference_paper_pdf_url,
    ):
        selector = Selector(
            text=xml_record,
            type="xml"
        )
        selector.remove_namespaces()
        record = HEPLoader(
            item=HEPRecord(),
            selector=selector
        )

        license_text = selector.xpath(
            './/metadata/pex-dc/rights/text()'
        ).extract_first()
        record.add_value('license', get_licenses(license_text=license_text))

        date, year = self._get_date(selector=selector)
        record.add_value('date_published', date)
        record.add_value('journal_year', year)

        identifier = selector.xpath(
            ".//metadata/pex-dc/identifier/text()"
        ).extract_first()
        record.add_value(
            'journal_title',
            self._get_journal_title(pos_ext_identifier=identifier),
        )
        record.add_value(
            'journal_volume',
            self._get_journal_volume(pos_ext_identifier=identifier),
        )
        record.add_value(
            'journal_artid',
            self._get_journal_artid(pos_ext_identifier=identifier),
        )

        record.add_xpath('title', '//metadata/pex-dc/title/text()')
        record.add_xpath('source', '//metadata/pex-dc/publisher/text()')
        record.add_value(
            'external_system_numbers',
            self._get_ext_systems_number(selector=selector),
        )
        record.add_value('language', self._get_language(selector=selector))
        record.add_value('authors', self._get_authors(selector=selector))
        record.add_value('collections', ['conferencepaper'])
        record.add_value('urls', [conference_paper_url])
        record.add_value(
            '_fft',
            self._set_fft(
                path=conference_paper_pdf_url,
            ),
        )

        parsed_item = ParsedItem(
            record=record.load_item(),
            record_format='hepcrawl',
        )

        return parsed_item

    def build_conference_proceedings_item(
        self,
        proceedings_page_html,
        pos_id,
    ):
        selector = Selector(
            text=proceedings_page_html,
            type='html',
        )
        selector.remove_namespaces()
        record = HEPLoader(
            item=HEPRecord(),
            selector=selector
        )

        record.add_value('collections', ['proceeding'])
        record.add_value(
            'title',
            self._get_proceedings_title(selector=selector),
        )
        record.add_value(
            'subtitle',
            self._get_proceedings_date_place(selector=selector),
        )
        record.add_value('journal_title', 'PoS')
        record.add_value(
            'journal_volume',
            self._get_journal_volume(pos_ext_identifier=pos_id),
        )

        parsed_proceeding = ParsedItem(
            record=record.load_item(),
            record_format='hepcrawl',
        )

        return parsed_proceeding

    def _get_conference_paper_pdf_url(self, conference_paper_page_html):
        selector = Selector(
            text=conference_paper_page_html,
            type='html',
        )
        conference_paper_pdf_relative_url = selector.xpath(
            "//a[contains(text(),'pdf')]/@href",
        ).extract_first()

        if not conference_paper_pdf_relative_url:
            raise PoSExtractionException(
                (
                    'Unable to get the conference paper pdf url from the html:'
                    '\n%s'
                ) % conference_paper_page_html
            )

        return urljoin(
            self.base_conference_paper_url,
            conference_paper_pdf_relative_url,
        )

    def _get_proceedings_url(self, response):
        internal_url = response.selector.xpath(
            "//a[not(contains(text(),'pdf'))]/@href",
        ).extract_first()
        proceedings_identifier = internal_url.split('/')[1]
        return '{0}{1}'.format(self.BASE_PROCEEDINGS_URL, proceedings_identifier)

    @staticmethod
    def _set_fft(path):
        return [
            {
                'path': path,
            },
        ]

    @staticmethod
    def _get_language(selector):
        language = selector.xpath(
            ".//metadata/pex-dc/language/text()"
        ).extract_first()
        return language if language != 'en' else None

    @staticmethod
    def _get_journal_title(pos_ext_identifier):
        return re.split('[()]', pos_ext_identifier)[0]

    @staticmethod
    def _get_journal_volume(pos_ext_identifier):
        return re.split('[()]', pos_ext_identifier)[1]

    @staticmethod
    def _get_journal_artid(pos_ext_identifier):
        return re.split('[()]', pos_ext_identifier)[2]

    @staticmethod
    def _get_ext_systems_number(selector):
        return [
            {
                'institute': 'pos',
                'value': selector.xpath(
                    './/identifier/text()'
                ).extract_first()
            },
        ]

    @staticmethod
    def _get_date(selector):
        full_date = selector.xpath(
            ".//metadata/pex-dc/date/text()"
        ).extract_first()
        date = create_valid_date(full_date)
        year = int(date[0:4])

        return date, year

    @staticmethod
    def _get_authors(selector):
        """Get article authors."""
        authors = []
        creators = selector.xpath('.//metadata/pex-dc/creator')
        for creator in creators:
            auth_dict = {}
            author = Selector(text=creator.extract())
            auth_dict['raw_name'] = get_first(
                author.xpath('.//name//text()').extract(),
                default='',
            )
            for affiliation in author.xpath(
                './/affiliation//text()'
            ).extract():
                if 'affiliations' in auth_dict:
                    auth_dict['affiliations'].append(
                        {
                            'value': affiliation
                        }
                    )
                else:
                    auth_dict['affiliations'] = [
                        {
                            'value': affiliation
                        },
                    ]
            if auth_dict:
                authors.append(auth_dict)
        return authors

    @staticmethod
    def _get_proceedings_title(selector):
        return selector.xpath('//h1/text()').extract_first()

    @staticmethod
    def _get_proceedings_date_place(selector):
        date_place = selector.xpath(
            "//div[@class='conference_date']/text()"
        ).extract()
        return ''.join(date_place)
