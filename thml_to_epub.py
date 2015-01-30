#!/usr/bin/env python

from collections import defaultdict
import argparse
import itertools
import mimetypes
import os.path
import re
import sys
import time
import urllib
import urlparse
import uuid
import zipfile

from lxml import etree
import requests


###### ThML to HTML conversion ######

### Constants and sentinels ###

REMOVE = object()
ADD = object()
COPY = object()

UNHANDLED = object()
DESCEND = object()
FINISHED = object()
HANDLED = [DESCEND, FINISHED]

### etree utilities ###

def add_text(node, text):
    if text is None:
        return
    if node.text is None:
        node.text = text
    else:
        node.text += text

def add_tail(node, tail):
    if tail is None:
        return
    if node.tail is None:
        node.tail = tail
    else:
        node.tail += tail

def append_text(parent, text):
    c = parent.getchildren()
    if c:
        add_tail(c[-1], text)
    else:
        add_text(parent, text)

### Utils ###

def dplus(d1, d2):
    """
    'Add' two dictionaries together and return the output.
    """
    out = d1.copy()
    out.update(d2)
    return out

def utf8(text):
    if isinstance(text, unicode):
        return text.encode('utf-8')
    else:
        return text

def html_escape(text):
    return (utf8(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;'))

### Handler classes ###

# Attribute default map:
ADEFS = {
    'style': REMOVE, # often just causes problems
    'id': COPY,
    'class': COPY,
    'lang': COPY,
    'title': COPY,
    'dir': COPY,
}

# Base class
class Handler(object):
    post_process_sort_order = 0

    def match_attributes(self, attribs):
        return True

    def match(self, from_node):
        return (self.from_node_name == '*' or from_node.tag == self.from_node_name) and \
            self.match_attributes(from_node.attrib)

    def post_process(self, converter, output_dom):
        pass

    def handle_node(self, converter, from_node, output_parent):
        # This method should add everything necessary
        # to output_parent (which is an ElementTree node of the
        # parent node in the output document) from
        # 'from_node' (but not its child nodes). So, for example,
        # a Handler that maps a single node to a single node
        # will create just one Element from the current node.

        # This method must then return a tuple containing:
        #  descend, node
        #
        # where:
        #
        # descend is a flag saying whether the conversion process should descend
        # into child nodes.
        #
        # node is the node that should be used as the parent node
        # for the child nodes.
        raise NotImplementedError()


def add_attrib_matcher(nodehandler, attrib_matcher):
    """
    Adds a 'match_attributes' method to a Handler class from a matcher function.
    """
    if attrib_matcher is not None:
        nodehandler.match_attributes = lambda self, attribs: attrib_matcher(attribs)

def UNWRAP(node_name):
    """
    Returns a Handler that unwraps a node, yanking children up.
    """
    class nodehandler(Handler):
        def handle_node(self, converter, from_node, output_parent):
            # Care with text and tail
            append_text(output_parent, from_node.text)
            append_text(output_parent, from_node.tail)
            return True, output_parent

    nodehandler.from_node_name = node_name
    nodehandler.__name__ = 'UNWRAP({0})'.format(node_name)
    return nodehandler


def READ(node_name):
    """
    Returns a Handler that does nothing with a node
    except read its children
    """
    class nodehandler(Handler):
        def handle_node(self, converter, from_node, output_parent):
            return True, output_parent

    nodehandler.from_node_name = node_name
    nodehandler.__name__ = "READ({0})".format(node_name)
    return nodehandler


def DELETE(node_name, attrib_matcher=None):
    """
    Returns a Handler that deletes a node (including children)
    """
    class nodehandler(Handler):
        def handle_node(self, converter, from_node, output_parent):
            # We have preserve 'tail' text
            append_text(output_parent, from_node.tail)
            return False, None

    nodehandler.__name__ = 'DELETE({0})'.format(node_name)
    nodehandler.from_node_name = node_name
    add_attrib_matcher(nodehandler, attrib_matcher)
    return nodehandler


# lxml throws errors with .sourceline sometimes
def get_sourceline(node):
    try:
        return node.sourceline
    except (ValueError, AttributeError):
        return '?'

def set_sourceline(node, line):
    try:
        node.sourceline = line
    except:
        pass

def MAP(from_node_name, to_node_name, attribs, attrib_matcher=None):
    """Returns a Handler that maps from one node to another,
    with handled attributes given in attribs.

    attribs should be a dictionary mapping attribute names in source using
    REMOVE or COPY constants. It can also have an ADD key which is handled
    specially - it should be a list of attributes to add as (name, value)
    pairs.

    """
    class nodehandler(Handler):
        def handle_node(self, converter, from_node, output_parent):
            e = etree.Element(self.to_node_name)
            e.text = from_node.text
            e.tail = from_node.tail
            output_parent.append(e)
            # Handle attributes
            for k, v in from_node.attrib.items():
                if k not in self.attribs:
                    sys.stderr.write("WARNING: ignoring unknown attribute {0} on {1} node, line {2}\n".format(k, from_node.tag, get_sourceline(from_node)))
                else:
                    replacement = self.attribs[k]
                    if replacement is COPY:
                        e.set(k, v)
                    elif replacement is REMOVE:
                        pass
                    else:
                        raise Exception("Replacement {0} not understood".format(repr(replacement)))
            if ADD in self.attribs:
                for k, v in self.attribs[ADD]:
                    e.set(k, v)

            return True, e

    nodehandler.__name__ = 'MAP({0}, {1})'.format(from_node_name, to_node_name)
    nodehandler.from_node_name = from_node_name
    nodehandler.to_node_name = to_node_name
    nodehandler.attribs = attribs
    add_attrib_matcher(nodehandler, attrib_matcher)

    return nodehandler


def DIV(from_node_name, to_node_name, attribs):
    cls = MAP(from_node_name, to_node_name, attribs)
    class divhandler(cls):
        def handle_node(self, converter, from_node, output_parent):
            title = from_node.attrib.get('title', None)
            descend, node = super(divhandler, self).handle_node(converter, from_node, output_parent)

            # Collect info for headings/TOC. Note that there will be multiple
            # divhandler classes and therefore multiple instances, so we have to
            # put shared state onto converter instead of self.
            if node is not None and title is not None:
                converter.toc.count += 1
                id = node.attrib.get('id', '_gentocid_{0}'.format(converter.toc.count))
                node.set('id', id)
                item = TocItem(title, id, [])

                # Now need to figure out which parent it belongs to.
                n = node.getparent()
                parent_toc_item = None
                while n is not None:
                    if n in converter.toc.node_map:
                        parent_toc_item = converter.toc.node_map[n]
                        break
                    n = n.getparent()
                if parent_toc_item is None:
                    parent_toc_list = converter.toc.items
                else:
                    parent_toc_list = parent_toc_item.children
                parent_toc_list.append(item)
                converter.toc.node_map[node] = item

            return descend, node

    return divhandler


class AnchorHandler(MAP('a', 'a', dplus(ADEFS, {'href': COPY, 'name': COPY}))):
    def handle_node(self, converter, from_node, output_parent):
        descend, node = super(AnchorHandler, self).handle_node(converter, from_node, output_parent)
        if node is not None:
            if 'href' in node.attrib:
                href = node.attrib['href']
                p = urlparse.urlparse(href)
                if not p.netloc and p.fragment:
                    # Strip query - only want fragment
                    href = '#' + p.fragment
                    node.attrib['href'] = href
        return descend, node


class ImgHandler(MAP('img', 'img', dplus(ADEFS, {'src': COPY, 'alt': COPY, 'height': COPY, 'width': COPY}))):
    def __init__(self):
        super(ImgHandler, self).__init__()
        self.img_srcs = set()

    def handle_node(self, converter, from_node, output_parent):
        descend, node = super(ImgHandler, self).handle_node(converter, from_node, output_parent)
        if 'src' in node.attrib:
            src = node.attrib['src']
            url = urlparse.urlparse(src)
            # Create a relative path, 1 path component
            filename = os.path.split(url.path)[-1]
            node.attrib['src'] = filename
            if converter.download_images:
                self.img_srcs.add((filename, src))
        return descend, node

    def post_process(self, converter, output_dom):
        converter.img_files = []
        image_directory = converter.image_directory
        ccel_book_url = None
        for n, d in converter.metadata.get('dc:identifier', []):
            if d.get('scheme', '') == 'URL':
                ccel_book_url = n

        ccel_url_base = 'http://www.ccel.org'
        if ccel_book_url is not None:
            book_path = ccel_book_url.replace('.html', '')
            book_img_base = ccel_url_base + book_path + '/files/'
        else:
            book_path = None
            book_img_base = None

        # Get all images
        for filename, src in sorted(list(self.img_srcs)):
            found = False
            if not converter.ignore_downloaded_images and image_directory:
                path = os.path.join(image_directory, filename)
                if os.path.exists(path):
                    sys.stderr.write("SUCCESS: {0} found at {1}.\n".format(filename, path))
                    converter.img_files.append({
                        'file_name': filename,
                        'media_type': mimetypes.guess_type(path),
                        'content': file(path).read(),
                        })
                    found = True

            if found:
                continue

            # Download:

            attempts = []
            if book_img_base is not None and not src.startswith('/'):
                attempts.append(book_img_base + src)
            if src.startswith('/'):
                attempts.append(ccel_url_base + src)

            if book_path is not None:
                stem, ext = os.path.splitext(filename)
                ext = ext[1:]
                m = re.match('.*-p(\d+)', stem)
                if m:
                    # Looks like it could be a page image.
                    # Attempt to get image from page scans.
                    pagenum = int(m.groups()[0])
                    new_attempt = 'http://www.ccel.org{path}/{ext}/{pagenum:04d}={pagenum}.{ext}'.format(
                        path=book_path, ext=ext, pagenum=pagenum)
                    attempts.append(new_attempt)

            for url in attempts:
                if found:
                    break
                img_file_resp = requests.get(url)
                if img_file_resp.status_code == 200:
                    if not img_file_resp.headers.get('content-type', '').startswith('image/'):
                        sys.stderr.write("WARNING: ignoring download for {0} which is not an image file.\n".format(url))
                    else:
                        sys.stderr.write("SUCCESS: {0} found at {1}.\n".format(filename, url))
                        converter.img_files.append({
                            'file_name': filename,
                            'media_type': img_file_resp.headers['content-type'],
                            'content': img_file_resp.content,
                        })
                        if image_directory:
                            path = os.path.join(image_directory, filename)
                            if not os.path.exists(image_directory):
                                os.makedirs(image_directory)
                            with file(path, "w") as f:
                                f.write(img_file_resp.content)
                        found = True
                else:
                    sys.stderr.write("WARNING: Image download: {0} for {1}\n".format(img_file_resp.status_code, url))
                time.sleep(converter.http_sleep_time)


class CollectNodesMixin(object):
    def __init__(self):
        super(CollectNodesMixin, self).__init__()
        self.collected_nodes = []

    def handle_node(self, converter, from_node, output_parent):
        descend, node = super(CollectNodesMixin, self).handle_node(converter, from_node, output_parent)
        if node is not None:
            self.collected_nodes.append(node)
        return descend, node


class LineHandler(CollectNodesMixin,
                  MAP('l', 'span', dplus(ADEFS, {ADD: [('class', 'line')]}))):
    def post_process(self, converter, output_dom):
        # Need a 'BR' to appear right at the end of the line
        for node in self.collected_nodes:
            node.append(etree.Element('br'))

def fix_passage_ref(ref):
    # TODO handle osisRef or passage better - expand abbreviations
    return utf8(ref).replace('.', ' ')

class ScripRefHandler(MAP('scripRef', 'a',
                          dplus(ADEFS, {'passage': REMOVE,
                                        'parsed': REMOVE,
                                        'version': REMOVE,
                                        'osisRef': REMOVE}))):
    def handle_node(self, converter, from_node, output_parent):
        descend, node = super(ScripRefHandler, self).handle_node(converter, from_node, output_parent)
        if node is not None and 'passage' in from_node.attrib:
            node.set('href',
                     'https://www.biblegateway.com/passage/?search={0}&version=NIV'.format(
                         urllib.quote(fix_passage_ref(from_node.attrib['passage']))))
        else:
            sys.stderr.write("WARNING: can't get 'passage' from scripRef attribs {0} on line {1}\n".format(from_node.attrib, get_sourceline(from_node)))
            node.set('href', '#')
        return descend, node


class NoteHandler(Handler):
    from_node_name = 'note'
    def __init__(self):
        self.notes = []
        self.generated_id_num = 0
        self.generated_anchor_id_num = 0

    def next_id(self):
        self.generated_id_num += 1
        return "_genid_{0}".format(self.generated_id_num)

    def next_anchor_id(self):
        self.generated_anchor_id_num += 1
        return "_genaid_{0}".format(self.generated_anchor_id_num)

    def handle_node(self, converter, from_node, output_parent):
        # Build note
        note_id = from_node.attrib.get('id', None)
        if note_id is None:
            note_id = self.next_id()
        note = etree.Element("div", {'id': note_id,
                                     'class': 'note'})
        set_sourceline(note, get_sourceline(from_node))

        # Build anchor
        anchor = etree.Element("a",
                               {'href': '#' + note_id,
                                'id': self.next_anchor_id(),
                            })
        set_sourceline(anchor, get_sourceline(from_node))
        anchor.tail = from_node.tail
        sup = etree.Element("sup")
        footnote_num = len(self.notes) + 1
        sup.text = "[{0}]".format(footnote_num)
        anchor.append(sup)
        output_parent.append(anchor)

        # Return anchor
        return_anchor = etree.Element('a',
                                      {'href': '#' + anchor.attrib['id']})
        return_anchor.text = "[^{0}]".format(footnote_num)
        return_anchor.tail = " "
        # Put the text of the note after the return anchor:
        note.append(return_anchor)
        add_tail(return_anchor, from_node.text)

        self.notes.append((anchor, note))
        return True, note # Need the children elements of <note> to be added

    def post_process(self, converter, output_dom):
        note_containers = {}

        for anchor, note in self.notes:
            div = find_outermost_div(anchor)
            if div is None:
                sys.stderr.write("WARNING: Can't find a div to place footnote for note on line {0}\n".format(get_sourceline(anchor)))
                continue
            if div not in note_containers:
                container = etree.Element('div', attrib={'class': 'notes'})
                div.append(container)
            else:
                container = note_containers[div]
            container.append(note)


def find_outermost_div(node, last_div=None):
    if node is None:
        return last_div
    if node.tag == 'div':
        last_div = node
    return find_outermost_div(node.getparent(), last_div=last_div)


class DCMetaDataCollector(Handler):
    post_process_sort_order = -100

    def __init__(self):
        self.dc_metadata = defaultdict(list)

    def match(self, from_node):
        parent = from_node.getparent()
        return parent is not None and parent.tag == "DC"

    def handle_node(self, converter, from_node, output_parent):
        if from_node.text is not None:
            item = (from_node.text, dict(from_node.attrib))
            name = from_node.tag.lower().replace('.', ':')
            if item not in self.dc_metadata[name]:
                self.dc_metadata[name].append(item)
        return False, None

    def post_process(self, converter, output_dom):
        if 'dc:title' in self.dc_metadata:
            # Insert a 'title' element into doc, it's required for HTML validity
            head = output_dom.find('head')
            if head is not None:
                title = etree.Element('title')
                title.text = self.dc_metadata['dc:title'][0][0]
                head.append(title)
        converter.metadata.update(self.dc_metadata)


class Fallback(UNWRAP('*')):
    pass


DIVADEFS = dplus(ADEFS,
                 {'n': REMOVE,
                  'shorttitle': REMOVE,
                  'title': REMOVE,
                  'progress': REMOVE,
                  'type': REMOVE,
                  'filebreak': REMOVE,
                  'prev': REMOVE,
                  'next': REMOVE,})

TADEFS =  dplus(ADEFS, {'align': COPY,
                        'valign': COPY,
                        'border': COPY,
                        'cellspacing': COPY,
                        'cellpadding': COPY,
                        'rowspan': COPY,
                        'colspan': COPY,
                        'width': COPY,
                    })

# Define set of classes that will handle the transformation.
HANDLERS = [
    # TODO ThML.head etc

    # We list all HTML element explicitly, even if they are the same in ThML and
    # HTML, because we want to make sure that we match everything so we can
    # produce valid XHTML.

    ## ThML elements
    MAP('ThML', 'html', {}),
    MAP('ThML.head', 'head', {}),
    MAP('ThML.body', 'body', {}),
    DIV('div1', 'div', DIVADEFS),
    DIV('div2', 'div', DIVADEFS),
    DIV('div3', 'div', DIVADEFS),
    DIV('div4', 'div', DIVADEFS),
    DIV('div5', 'div', DIVADEFS),
    MAP('verse', 'div', dplus(ADEFS, {ADD: [('class', 'verse')],
                                      'type': REMOVE,
                                  })),
    MAP('scripCom', 'div', dplus(ADEFS,
                                 {ADD: [('class', 'scripCom')],
                                  'parsed': REMOVE,
                                  'osisRef': REMOVE,
                                  'passage': REMOVE,
                                  'type': REMOVE,
                              })),
    MAP('scripture', 'blockquote', dplus(ADEFS, {'passage': REMOVE, 'parsed': REMOVE, 'osisRef': REMOVE})),
    LineHandler,
    ScripRefHandler,
    MAP('pb', 'br', dplus(ADEFS, {'n': REMOVE, 'href': REMOVE})),
    NoteHandler,
    MAP('name', 'span', ADEFS),
    MAP('attr', 'span', ADEFS),

    UNWRAP('unclear'),
    UNWRAP('added'),
    DELETE('deleted'),
    DELETE('insertIndex'), # TODO - create an index where it is missing?

    ## HTML elements
    # Header:
    MAP('title', 'title', {}),
    DELETE('link'),
    DELETE('script'),
    MAP('style', 'style', {'type':COPY}, attrib_matcher=lambda attrib: attrib.get('type', '')=='text/css'),
    DELETE('style', attrib_matcher=lambda attrib: attrib.get('type', '')=='text/xcss'),


    # Block
    MAP('p', 'p', ADEFS),
    MAP('div', 'div', ADEFS),
    MAP('h1', 'h1', ADEFS),
    MAP('h2', 'h2', ADEFS),
    MAP('h3', 'h3', ADEFS),
    MAP('h4', 'h4', ADEFS),
    MAP('h5', 'h5', ADEFS),
    MAP('h6', 'h6', ADEFS),
    MAP('table', 'table', TADEFS),
    MAP('tbody', 'tbody', TADEFS),
    MAP('thead', 'thead', TADEFS),
    MAP('colgroup', 'colgroup', TADEFS),
    MAP('col', 'col', TADEFS),
    MAP('rowgroup', 'rowgroup', TADEFS),
    MAP('row', 'row', TADEFS),
    MAP('tr', 'tr', TADEFS),
    MAP('td', 'td', TADEFS),
    MAP('th', 'th', TADEFS),
    MAP('br', 'br', ADEFS),
    ImgHandler,
    MAP('ul', 'ul', ADEFS),
    MAP('ol', 'ol', ADEFS),
    MAP('li', 'li', ADEFS),
    MAP('blockquote', 'blockquote', ADEFS),
    MAP('address', 'address', ADEFS),
    MAP('hr', 'hr', ADEFS),
    MAP('pre', 'pre', ADEFS),

    # Inline
    AnchorHandler,
    MAP('b', 'b', ADEFS),
    MAP('i', 'i', ADEFS),
    MAP('em', 'em', ADEFS),
    MAP('small', 'small', ADEFS),
    MAP('strong', 'strong', ADEFS),
    MAP('span', 'span', ADEFS),
    MAP('sub', 'sub', ADEFS),
    MAP('sup', 'sup', ADEFS),
    MAP('abbr', 'abbr', ADEFS),
    MAP('cite', 'cite', ADEFS),

    # TODO ... maps for every element we want to handle

    # Collectors for metadata
    READ('DC'),
    READ('electronicEdInfo'),
    DCMetaDataCollector,

    DELETE('generalInfo'),
    DELETE('comments'),
    DELETE('printSourceInfo'),
    DELETE('publisherID'),
    DELETE('authorID'),
    DELETE('bookID'),
    DELETE('version'),
    DELETE('series'),
    DELETE('editorialComments'),
    DELETE('revisionHistory'),
    DELETE('status'),
    # Collectors for TOC

    # Handling note

    # Handling scripContext if possible

]


class HtmlDoc(object):
    def __init__(self, html, toc):
        self.html, self.toc = html, toc


class TocItem(object):
    def __init__(self, title, id, children):
        self.title, self.id, self.children = title, id, children

    def __eq__(self, other):
        return self.title == other.title and self.id == other.id\
            and len(self.children) == len(other.children)\
            and all(c1 == c2 for c1, c2 in zip(self.children, other.children))

    def __repr__(self):
        return "TocItem({0}, {1}, {2})".format(repr(self.title),
                                               repr(self.id),
                                               repr(self.children))

class Toc(object):
    def __init__(self):
        self.items = []
        self.count = 0
        self.node_map = {}


DOCTYPE = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n"""

class ThmlToHtml(object):
    def __init__(self, download_images=False, http_sleep_time=1, image_directory="", ignore_downloaded_images=False):
        self.download_images = download_images
        self.http_sleep_time = http_sleep_time
        self.image_directory = image_directory
        self.ignore_downloaded_images = ignore_downloaded_images
        self.handlers = [cls() for cls in HANDLERS]
        self.metadata = {}
        self.fallback = Fallback()

    def transform(self, thml, full_xml=False):
        self.toc = Toc() # reset for each document
        input_root = etree.fromstring(thml)
        output_root = etree.Element('root') # Temporary container that we will strip again
        self.descend(input_root, output_root)
        children = output_root.getchildren()
        assert len(children) == 1
        output_dom = children[0]
        self.post_process(output_dom)
        if full_xml:
            output_dom.set('xmlns', "http://www.w3.org/1999/xhtml")
        html = etree.tostring(output_dom,
                              encoding='utf-8',
                              doctype=DOCTYPE if full_xml else None,
                              xml_declaration=True if full_xml else None,
                              pretty_print=True)
        retval = HtmlDoc(html, self.toc)
        self.toc = None
        return retval

    def descend(self, input_node, output_parent_node):
        retvals = []
        matched = False
        for handler in self.handlers:
            if handler.match(input_node):
                matched = True
                retvals.append(handler.handle_node(self, input_node, output_parent_node))
        if not matched:
            sys.stderr.write("WARNING: Element {0} on line {1} not properly handled\n".format(input_node.tag, get_sourceline(input_node)))
            retvals.append(self.fallback.handle_node(self, input_node, output_parent_node))
        should_descend = any(d for d, n in retvals)
        if should_descend:
            assert all(d for d, n in retvals)
        if not should_descend:
            return
        new_parents = [n for d, n in retvals if n is not None]
        if len(new_parents) > 1:
            raise Exception("More than one parent node returned for {0} on line {1}".format(input_node.tag, get_sourceline(input_node)))
        if len(new_parents) == 0:
            raise Exception("No new parent defined for node {0} on line {1}".format(input_node.tag, get_sourceline(input_node)))
        new_parent = new_parents[0]

        for node in input_node.getchildren():
            self.descend(node, new_parent)

    def post_process(self, output_dom):
        for handler in sorted(self.handlers, key=lambda h: h.post_process_sort_order):
            handler.post_process(self, output_dom)


# Simple interface:
def thml_to_html(input_thml):
    return ThmlToHtml().transform(input_thml, full_xml=False).html


### HTML to epub ###

class EpubFile(object):
    def __init__(self, file_name, content):
        self.file_name = file_name
        self.base_name = os.path.basename(file_name)
        self.content = content

    def get_path_relative_to_file(self, other_file):
        return os.path.relpath(self.file_name, os.path.dirname(other_file.file_name))


class OpfFile(EpubFile):
    pass


class NcxFile(EpubFile):
    pass


class ContentFile(EpubFile):
    def __init__(self, file_name, content, media_type, toc, file_id):
        super(ContentFile, self).__init__(file_name, content)
        self.media_type = media_type
        self.toc = toc
        self.file_id = file_id


class ContentFileCollection(object):
    def __init__(self):
        self.files = []

    def __iter__(self):
        return iter(self.files)

    def append(self, file_name, content, media_type, toc):
        f = ContentFile(file_name, content, media_type, toc, "file_{0}".format(len(self.files) + 1))
        self.files.append(f)
        return f

    def __getitem__(self, idx):
        return self.files[idx]


CREATOR_ROLES = {
    'Author': 'aut',
    'Author of section': 'aut',
    'Author of Section': 'aut',
    'Author of Part': 'aut',
    'Editor': 'edt',
    'Adapter': 'adp',
    'Annotator': 'ann',
    'Arranger': 'arr',
    'Artist': 'art',
    'Associated name': 'asn',
    'Author': 'aut',
    'Bibliographic antecedent': 'ant',
    'Book producer': 'bkp',
    'Collaborator': 'clb',
    'Commentator': 'cmm',
    'Designer': 'dsr',
    'Editor': 'edt',
    'Illustrator': 'ill',
    'Lyricist': 'lyr',
    'Metadata contact': 'mdc',
    'Musician': 'mus',
    'Narrator': 'nrt',
    'Other': 'oth',
    'Photographer': 'pht',
    'Printer': 'prt',
    'Redactor': 'red',
    'Reviewer': 'rev',
    'Sponsor': 'spn',
    'Thesis advisor': 'ths',
    'Transcriber': 'trc',
    'Translator': 'trl',
    'Translator and Editor': 'trl',
}

def map_creator_role(thml_creator_sub):
    # For a given DC.Creator 'sub' value used in ThML docs, return the
    # Dublin Core creator 'role' value.
    if thml_creator_sub not in CREATOR_ROLES:
        sys.stderr.write("WARNING: Unhandled DC.Creator sub value '{0}'\n".format(thml_creator_sub))
        return "oth"
    return CREATOR_ROLES[thml_creator_sub]

def create_epub(input_html_pairs, metadata, img_files, outputfilename):
    content_files = ContentFileCollection()
    for i, (src_name, html_doc) in enumerate(input_html_pairs):
        content_files.append("OEBPS/{0}.html".format(i + 1), html_doc.html, "application/xhtml+xml", html_doc.toc)

    for img_file in img_files:
        content_files.append("OEBPS/" + img_file['file_name'], img_file['content'], img_file['media_type'], None)

    #### mimetype
    mimetype_file = EpubFile("mimetype", "application/epub+zip")
    opf_file, identifier_id, identifier_val, title = make_opf_file(content_files, metadata)
    container_file = make_container_file(opf_file)
    ncx_file = make_ncx_file(content_files, identifier_id, identifier_val, title)
    # Write epub

    epub = zipfile.ZipFile(outputfilename, "w", zipfile.ZIP_DEFLATED)
    for file in [mimetype_file, container_file, opf_file, ncx_file] + content_files.files:
        epub.writestr(file.file_name, file.content,
                      zipfile.ZIP_STORED if file.file_name == 'mimetype' else zipfile.ZIP_DEFLATED)

    epub.close()


def make_container_file(opf_file):
    container_file = EpubFile("META-INF/container.xml", '''<?xml version="1.0"?>
<container version="1.0"
           xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{0}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>'''.format(opf_file.file_name))
    return container_file


def make_opf_file(content_files, metadata):
    opf_file = OpfFile("OEBPS/content.opf", "")

    index_tpl = '''<?xml version='1.0' encoding='utf-8'?>
<package version="2.0"
         xmlns="http://www.idpf.org/2007/opf"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:opf="http://www.idpf.org/2007/opf"
         unique-identifier="{identifier_id}"
>
  <metadata>
    {metadata}
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    {manifest}
  </manifest>
  <spine toc="ncx">
    {spine}
  </spine>
</package>'''

    manifest = ""
    spine = ""

    ## Metadata:

    # First pass - gather some items to use later.
    creators = {}
    creators_file_as = {}
    identifier_val = None
    identifier_id = None
    title = "Untitled"
    for name, lst in metadata.items():
        for i, (value, attribs) in enumerate(lst):
            if name == 'dc:creator':
                role = map_creator_role(attribs.get('sub', ''))
                if attribs.get('scheme', '') == 'file-as':
                    creators_file_as[role] = value
                if attribs.get('scheme', '') == 'short-form':
                    creators[role] = value
            if name == 'dc:identifier':
                if 'id' not in attribs:
                    attribs['id'] = 'id{0}'.format(i)
                if i == 0:
                    # pick the first dc:identifier
                    identifier_id = attribs['id']
                    identifier_val = value
            if name == 'dc:title' and i == 0:
                title = value

    ## identifier
    if identifier_val is None:
        identifier_id = 'bookuuid'
        identifier_val =  uuid.uuid4().get_urn()
    if 'dc:identifier' not in metadata:
        metadata['dc:identifier'] = [(identifier_val, {'id': identifier_id})]

    ## creator
    metadata['dc:creator'] = []
    for role in dplus(creators, creators_file_as).keys():
        c = creators.get(role, None)
        c_file_as = creators_file_as.get(role, None)
        if c is None and c_file_as is not None:
            c = c_file_as
        elif c_file_as is None and c is not None:
            c_file_as = c

        metadata['dc:creator'].append((c, {'opf:file-as': c_file_as,
                                           'opf:role': role}))

    m = []
    for name, lst in metadata.items():
        for i, (value, attribs) in enumerate(lst):
            attribs = {k: v for k, v in attribs.items()
                       if k in ['id', 'opf:file-as', 'opf:role']}
            if attribs:
                attribs_html = ' ' + ' '.join('{0}="{1}"'.format(utf8(k), html_escape(v))
                                 for k, v in attribs.items())
            else:
                attribs_html = ''
            m.append('<{tag}{attribs_html}>{value}</{tag}>'.format(
                tag=name,
                attribs_html=attribs_html,
                value=html_escape(value)))
    metadata_str = '\n'.join(m)


    for f in content_files:
        manifest += '<item id="{0}" href="{1}" media-type="application/xhtml+xml"/>'.format(
            f.file_id, f.get_path_relative_to_file(opf_file))
        spine += '<itemref idref="{0}" linear="yes" />'.format(f.file_id)

    opf_file.content = index_tpl.format(
        identifier_id=identifier_id,
        manifest=manifest,
        spine=spine,
        metadata=metadata_str,
    )

    return opf_file, identifier_id, identifier_val, title


def make_ncx_file(content_files, identifier_id, identifier_val, title):

    #### TOC
    ncx_file = NcxFile("OEBPS/toc.ncx", "")
    depth, navpoints = make_nav_points(ncx_file, content_files)
    ncx_file.content = '''<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"
                 "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{identifier_val}"/>
    <meta name="dtb:depth" content="{depth}"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle>
    <text>{title}</text>
  </docTitle>
  <navMap>
    {navpoints}
  </navMap>
</ncx>'''.format(
    identifier_val=html_escape(identifier_val),
    title=html_escape(title),
    navpoints="\n".join(navpoints),
    depth=depth,
    )
    return ncx_file

def make_nav_points(ncx_file, content_files):
    max_depth = None
    all_points = []
    counter = itertools.count(1)
    for f in content_files:
        if f.toc is None:
            continue # image files
        depth, points = make_nav_points_helper(ncx_file, f, f.toc.items, counter, 0)
        all_points.extend(points)
        if max_depth is None:
            max_depth = depth
        else:
            max_depth = max(max_depth, depth)

    return depth, all_points

def make_nav_points_helper(ncx_file, content_file, toc_items, counter, depth):
    if len(toc_items) == 0:
        return depth, []

    tpl = """<navPoint id="navpoint-{count}" playOrder="{count}">
  <navLabel>
    <text>{title}</text>
  </navLabel>
  <content src="{src}"/>
  {navpoints}
</navPoint>
        """

    points = []
    for item in toc_items:
        child_depth, child_points = make_nav_points_helper(ncx_file, content_file, item.children, counter, depth)
        depth = max(depth, child_depth)
        pt = utf8(tpl.format(count=next(counter),
                             title=utf8(item.title),
                             src=content_file.get_path_relative_to_file(ncx_file) + "#" + item.id,
                             navpoints="\n".join(child_points)))
        points.append(pt)
    return depth + 1, points


### Main ###

parser = argparse.ArgumentParser()
parser.add_argument("thml_file", nargs='+')
parser.add_argument("--download-images", action='store_true',
                    help="Attempt to download images from CCEL. WORK IN PROGRESS")
parser.add_argument("--save-downloaded-images-to", default="%d/%f_files/",
                    help="""Folder to save downloaded images to. Defaults to %(default)s (see substitutions below).
This saves downloading same files over and over. Set to empty to disable.""")
parser.add_argument("--ignore-downloaded-images", default=False, action='store_true',
                    help="""Don't use previously downloaded images""")
parser.add_argument("--http-sleep-time", action='store', default=1, type=int,
                    help="Amount to sleep in seconds between HTTP requests when downloading, to avoid slamming CCEL")
parser.add_argument("--output", default="%d/%f.rough.epub",
                    help="""Template for the output filename. Default: %(default)s. Substitutions are:
%%d: directory of first input filename;
%%f: basename of first input filename without extension;
%%t: title extracted from metadata;
%%a: author extracted from metadata;
                     """)

def do_substitutions(template, directory, basename, metadata):
    if '%d' in template:
        template = template.replace('%d', os.path.abspath(directory))
    if '%f' in template:
        template = template.replace('%f', os.path.splitext(basename)[0])
    if '%t' in template:
        template = template.replace('%t', metadata['dc:title'][0][0])
    if '%a' in template:
        template = template.replace('%a', sorted(metadata['dc:creator'],
                                                 key=lambda (n, attrs):
                                                 0 if (attrs.get('sub', '').lower() == 'author' and attrs.get('scheme','') == 'file-as')
                                                 else 1)[0][0])

    return template


def main():
    args = parser.parse_args()
    input_files = args.thml_file
    input_thml_pairs = [(fn, file(fn).read()) for fn in input_files]
    directory = os.path.dirname(input_files[0])
    basename = os.path.basename(input_files[0])
    image_directory = do_substitutions(args.save_downloaded_images_to, directory, basename, None)
    converter = ThmlToHtml(download_images=args.download_images,
                           http_sleep_time=args.http_sleep_time,
                           image_directory=image_directory,
                           ignore_downloaded_images=args.ignore_downloaded_images)
    input_html_pairs = [(fn, converter.transform(t, full_xml=True)) for fn, t in input_thml_pairs]
    outputfile = do_substitutions(args.output, directory, basename, converter.metadata)
    create_epub(input_html_pairs, converter.metadata, getattr(converter, 'img_files', []), outputfile)


### Tests ###

def test_elems():
    assert thml_to_html('<ThML></ThML>').strip() == \
        '<html/>'
    assert thml_to_html('<ThML>Hello</ThML>').strip() == \
        '<html>Hello</html>'
    assert thml_to_html('<ThML><p>Hello</p></ThML>').strip() == \
        '<html>\n  <p>Hello</p>\n</html>'
    assert thml_to_html('<ThML>Some <deleted>deleted</deleted>text</ThML>').strip() == \
        '<html>Some text</html>'
    assert thml_to_html('<ThML>Some <b>not deleted text</b> and <deleted>deleted</deleted>text</ThML>').strip() == \
        '<html>Some <b>not deleted text</b> and text</html>'
    assert thml_to_html('<ThML>Some <deleted><b>really deleted</b></deleted>text</ThML>').strip() == \
        '<html>Some text</html>'
    assert thml_to_html('<ThML>Some <added>added</added> text</ThML>').strip() == \
        '<html>Some added text</html>'
    assert thml_to_html('<ThML><added>Some added text</added></ThML>').strip() == \
        '<html>Some added text</html>'
    assert thml_to_html('<ThML>Some <b>bold</b> and <added>added</added> text</ThML>').strip() == \
        '<html>Some <b>bold</b> and added text</html>'
    assert thml_to_html('<ThML>Some <added>added <b>and bold</b> text</added></ThML>').strip() == \
        '<html>Some added <b>and bold</b> text</html>'
    assert thml_to_html('<ThML><l>A line</l></ThML>').strip() == \
        '<html>\n  <span class="line">A line<br/></span>\n</html>'
    assert thml_to_html('<ThML><ThML.head><title>The Title</title></ThML.head></ThML>').strip() == \
        '<html>\n  <head>\n    <title>The Title</title>\n  </head>\n</html>'
    assert thml_to_html('<ThML><style type="text/css">foo</style></ThML>').strip() == \
        '<html>\n  <style type="text/css">foo</style>\n</html>'
    assert thml_to_html('<ThML><style type="text/xcss">foo</style></ThML>').strip() == \
        '<html/>'

def test_divs():
    assert thml_to_html('<ThML><div1><div2>Some text</div2>And more</div1></ThML>').strip() == \
        '<html>\n  <div><div>Some text</div>And more</div>\n</html>'

def test_attribs():
    assert thml_to_html('<ThML><p id="foo">Hi</p></ThML>').strip() == \
        '<html>\n  <p id="foo">Hi</p>\n</html>'
    assert thml_to_html('<ThML><verse>line</verse></ThML>').strip() == \
        '<html>\n  <div class="verse">line</div>\n</html>'
    assert thml_to_html('<ThML><pb n="ii" id="i"/></ThML>').strip() == \
        '<html>\n  <br id="i"/>\n</html>'

def test_notes():
    assert (thml_to_html('<ThML><div1><p>Peter<note>a <i>complete</i> idiot</note> said...</p></div1></ThML>').strip() ==
            '<html>\n'
            '  <div>\n'
            '    <p>Peter<a href="#_genid_1" id="_genaid_1"><sup>[1]</sup></a> said...</p>\n'
            '    <div class="notes">\n'
            '      <div class="note" id="_genid_1"><a href="#_genaid_1">[^1]</a> a <i>complete</i> idiot</div>\n'
            '    </div>\n'
            '  </div>\n'
            '</html>')

def test_metadata():
    converter = ThmlToHtml()
    html = converter.transform("""<ThML>
<ThML.head>
<generalInfo>
 <description/>
 <firstPublished/>
 <pubHistory/>
 <comments/>
</generalInfo>

<printSourceInfo>
 <published>Mickey Mouse Press, 1950</published>
</printSourceInfo>

<electronicEdInfo>
 <publisherID>abcd</publisherID>
 <authorID>daffy</authorID>
 <bookID>fribble</bookID>
 <version>1.0</version>
 <series/>
 <editorialComments/>
 <revisionHistory/>
 <status>Some status.</status>

 <DC>
 <DC.Title>Interesting Things</DC.Title>
 <DC.Creator sub="Author" scheme="file-as">Daffy Duck</DC.Creator>
 <DC.Creator sub="Author" scheme="short-form">D. Duck</DC.Creator>
 <DC.Creator sub="Author" scheme="abcd">daffy</DC.Creator>
 </DC>
</electronicEdInfo>
</ThML.head>
</ThML>
""").html
    assert '<title>Interesting Things</title>' in html
    assert converter.metadata['dc:title'] == [("Interesting Things", {})]
    assert converter.metadata['dc:creator'] == [("Daffy Duck", {'sub': 'Author',
                                                                'scheme': 'file-as'}),
                                                ("D. Duck", {'sub': 'Author',
                                                             'scheme': 'short-form'}),
                                                ("daffy", {'sub': 'Author',
                                                           'scheme': 'abcd'})]

def test_toc_extraction():
    converter = ThmlToHtml()
    doc = converter.transform("""<ThML>
<ThML.body>
<div1 title="Chapter 1">
<p>Some intro stuff</p>
<div2 title="Section 1">
<p>Some stuff</p>
</div2>
<div2 title="Section 2">
<p>More stuff</p>
</div2>
</div1>
<div1 title="Chapter 2">
<h1>Hi</h1>
</div1>
</ThML.body>
</ThML>""")

    assert doc.html.strip() == """
<html>
<body>
<div id="_gentocid_1">
<p>Some intro stuff</p>
<div id="_gentocid_2">
<p>Some stuff</p>
</div>
<div id="_gentocid_3">
<p>More stuff</p>
</div>
</div>
<div id="_gentocid_4">
<h1>Hi</h1>
</div>
</body>
</html>""".strip()
    assert doc.toc.items == [
        TocItem("Chapter 1", "_gentocid_1", [
            TocItem("Section 1", "_gentocid_2", []),
            TocItem("Section 2", "_gentocid_3", []),
            ]),
        TocItem("Chapter 2", "_gentocid_4", [])
        ]

if __name__ == '__main__':
    main()
