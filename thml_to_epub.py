#!/usr/bin/env python

import sys
import urllib
import zipfile

from lxml import etree


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

### Handler classes ###

# Attribute default map:
ADEFS = {
    'style': COPY,
    'id': COPY,
    'class': COPY,
    'lang': COPY,
    'title': COPY,
}

# Base class
class Handler(object):
    def match_attributes(self, attribs):
        if not hasattr(self, 'attrib_matcher'):
            return True
        else:
            return self.attrib_matcher(attribs)

    def match(self, from_node):
        return (self.from_node_name == '*' or from_node.tag == self.from_node_name) and \
            self.match_attributes(from_node.attrib)

    def post_process(self, runner, output_dom):
        pass

    def handle_node(self, runner, from_node, output_parent):
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
    if attrib_matcher is not None:
        nodehandler.attrib_matcher = lambda self, attribs: attrib_matcher(attribs)

def UNWRAP(node_name):
    """
    Returns a Handler that unwraps a node, yanking children up.
    """
    class nodehandler(Handler):
        def handle_node(self, runner, from_node, output_parent):
            # Care with text and tail
            append_text(output_parent, from_node.text)
            append_text(output_parent, from_node.tail)
            return True, output_parent

    nodehandler.from_node_name = node_name
    nodehandler.__name__ = 'UNWRAP({0})'.format(node_name)
    return nodehandler


def DELETE(node_name, attrib_matcher=None):
    """
    Returns a Handler that deletes a node (including children)
    """
    class nodehandler(Handler):
        def handle_node(self, runner, from_node, output_parent):
            # We have preserve 'tail' text
            append_text(output_parent, from_node.tail)
            return False, None

    nodehandler.__name__ = 'DELETE({0})'.format(node_name)
    nodehandler.from_node_name = node_name
    add_attrib_matcher(nodehandler, attrib_matcher)
    return nodehandler


def MAP(from_node_name, to_node_name, attribs, attrib_matcher=None):
    """Returns a Handler that maps from one node to another,
    with handled attributes given in attribs.

    attribs should be a dictionary mapping attribute names in source using
    REMOVE or COPY constants. It can also have an ADD key which is handled
    specially - it should be a list of attributes to add as (name, value)
    pairs.

    """
    class nodehandler(Handler):
        def handle_node(self, runner, from_node, output_parent):
            e = etree.Element(self.to_node_name)
            e.text = from_node.text
            e.tail = from_node.tail
            output_parent.append(e)
            # Handle attributes
            for k, v in from_node.attrib.items():
                if k not in self.attribs:
                    sys.stderr.write("WARNING: ignoring unknown attribute {0} on {1} node, line {2}\n".format(k, from_node.tag, from_node.sourceline))
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
        def handle_node(self, runner, from_node, output_parent):
            retval = super(divhandler, self).handle_node(runner, from_node, output_parent)
            # TODO - collect info for headings/TOC
            return retval
        def post_process(self, runner, output_dom):
            pass # TODO - create TOC
    return divhandler

class CollectNodesMixin(object):
    def __init__(self):
        super(CollectNodesMixin, self).__init__()
        self.collected_nodes = []

    def handle_node(self, runner, from_node, output_parent):
        descend, node = super(CollectNodesMixin, self).handle_node(runner, from_node, output_parent)
        if node is not None:
            self.collected_nodes.append(node)
        return descend, node


class LineHandler(CollectNodesMixin,
                  MAP('l', 'span', dplus(ADEFS, {ADD: [('class', 'line')]}))):
    def post_process(self, runner, output_dom):
        # Need a 'BR' to appear right at the end of the line
        for node in self.collected_nodes:
            node.append(etree.Element('br'))

def fix_passage_ref(ref):
    # TODO handle osisRef or passage better - expand abbreviations
    return ref.replace('.', ' ')

class ScripRefHandler(MAP('scripRef', 'a',
                          dplus(ADEFS, {'passage': REMOVE, 'parsed': REMOVE, 'osisRef': REMOVE}))):
    def handle_node(self, runner, from_node, output_parent):
        descend, node = super(ScripRefHandler, self).handle_node(runner, from_node, output_parent)
        if node is not None and 'passage' in from_node.attrib:
            node.set('href',
                     'https://www.biblegateway.com/passage/?search={0}&version=NIV'.format(
                         urllib.quote(fix_passage_ref(from_node.attrib['passage']))))
        else:
            sys.stdout.write("WARNING: can't get 'passage' from scripRef attribs {0} on line {1}\n".format(from_node.attrib, from_node.sourceline))
            node.set('href', '#')
        return descend, node

class Fallback(UNWRAP('*')):
    pass


DIVADEFS = dplus(ADEFS,
                 {'n': REMOVE,
                  'shorttitle': REMOVE,
                  'progress': REMOVE,
                  'prev': REMOVE,
                  'next': REMOVE,})
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

    MAP('verse', 'div', dplus(ADEFS, {ADD: [('class', 'verse')]})),
    MAP('scripCom', 'div', dplus(ADEFS, {ADD: [('class', 'scripCom')]})),
    LineHandler,
    ScripRefHandler,
    MAP('pb', 'br', dplus(ADEFS, {'n': REMOVE, 'href': REMOVE})),

    UNWRAP('added'),
    DELETE('deleted'),

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
    MAP('table', 'table', ADEFS),
    MAP('tr', 'tr', ADEFS),
    MAP('td', 'td', ADEFS),
    MAP('th', 'th', ADEFS),
    MAP('br', 'br', ADEFS),
    MAP('img', 'img', dplus(ADEFS, {'src': COPY, 'alt': COPY, 'height': COPY, 'width': COPY})),
    MAP('ul', 'ul', ADEFS),
    MAP('ol', 'ol', ADEFS),
    MAP('li', 'li', ADEFS),
    MAP('blockquote', 'blockquote', ADEFS),
    MAP('address', 'address', ADEFS),
    MAP('hr', 'hr', ADEFS),

    # Inline
    MAP('a', 'a', dplus(ADEFS, {'href': COPY, 'name': COPY})),
    MAP('b', 'b', ADEFS),
    MAP('i', 'i', ADEFS),
    MAP('em', 'em', ADEFS),
    MAP('strong', 'strong', ADEFS),
    MAP('span', 'span', ADEFS),

    # TODO ... maps for every element we want to handle

    # Collectors for metadata

    # Collectors for TOC

    # Handling note

    # Handling scripContext if possible

]

DOCTYPE = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n"""

class ThmlToHtml(object):
    def __init__(self):
        self.handlers = [cls() for cls in HANDLERS]

    def transform(self, thml, full_xml=False):
        input_root = etree.fromstring(thml)
        output_root = etree.Element('root') # Temporary container that we will strip again
        self.fallback = Fallback()
        self.descend(input_root, output_root)
        children = output_root.getchildren()
        assert len(children) == 1
        output_dom = children[0]
        self.post_process(output_dom)
        if full_xml:
            output_dom.set('xmlns', "http://www.w3.org/1999/xhtml")
        return etree.tostring(output_dom,
                              encoding='utf-8',
                              doctype=DOCTYPE if full_xml else None,
                              xml_declaration=True if full_xml else None,
                              pretty_print=True)

    def descend(self, input_node, output_parent_node):
        retvals = []
        matched = False
        for handler in self.handlers:
            if handler.match(input_node):
                matched = True
                retvals.append(handler.handle_node(self, input_node, output_parent_node))
        if not matched:
            sys.stderr.write("WARNING: Element {0} on line {1} not properly handled\n".format(input_node.tag, input_node.sourceline))
            retvals.append(self.fallback.handle_node(self, input_node, output_parent_node))
        should_descend = any(d for d, n in retvals)
        if should_descend:
            assert all(d for d, n in retvals)
        if not should_descend:
            return
        new_parents = [n for d, n in retvals if n is not None]
        if len(new_parents) > 1:
            raise Exception("More than one parent node returned for {0} on line {1}".format(input_node.tag, input_node.sourceline))
        if len(new_parents) == 0:
            raise Exception("No new parent defined for node {0} on line {1}".format(input_node.tag, input_node.sourceline))
        new_parent = new_parents[0]

        for node in input_node.getchildren():
            self.descend(node, new_parent)

    def post_process(self, output_dom):
        for handler in self.handlers:
            handler.post_process(self, output_dom)


# Simple interface:
def thml_to_html(input_thml):
    return ThmlToHtml().transform(input_thml, full_xml=False)


### HTML to epub ###


def create_epub(input_html_pairs, outputfilename):
    epub = zipfile.ZipFile(outputfilename, "w", zipfile.ZIP_DEFLATED)

    epub.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
    # We need an index file, that lists all other HTML files
    # This index file itself is referenced in the META_INF/container.xml
    # file
    epub.writestr("META-INF/container.xml", '''<container version="1.0"
               xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles>
        <rootfile full-path="OEBPS/Content.opf" media-type="application/oebps-package+xml"/>
      </rootfiles>
    </container>''', zipfile.ZIP_STORED);

    # The index file is another XML file, living per convention
    # in OEBPS/Content.xml
    index_tpl = '''<package version="2.0"
      xmlns="http://www.idpf.org/2007/opf">
      <metadata/>
      <manifest>
        %(manifest)s
      </manifest>
      <spine toc="ncx">
        %(spine)s
      </spine>
    </package>'''

    manifest = ""
    spine = ""

    # Write each HTML file to the ebook, collect information for the index
    for i, (src_name, html_data) in enumerate(input_html_pairs):
        basename = "{0}.html".format(src_name)
        manifest += '<item id="file_%s" href="%s" media-type="application/xhtml+xml"/>' % (
            i+1, basename)
        spine += '<itemref idref="file_%s" />' % (i+1)
        epub.writestr('OEBPS/' + basename, html_data, zipfile.ZIP_DEFLATED)

    # Finally, write the index
    epub.writestr('OEBPS/Content.opf', index_tpl % {
      'manifest': manifest,
      'spine': spine,
    })
    epub.close()


### Main
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("thml_file", nargs='+')

def main():
    args = parser.parse_args()
    input_files = args.thml_file
    outputfile = input_files[0].replace('.xml', '').replace('.thml', '') + ".rough.epub"

    sys.stdout.write("Creating {0}\n".format(outputfile))
    input_thml_pairs = [(fn, file(fn).read()) for fn in input_files]
    transformer = ThmlToHtml()
    input_html_pairs = [(fn, transformer.transform(t, full_xml=True)) for fn, t in input_thml_pairs]
    create_epub(input_html_pairs, outputfile)


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

if __name__ == '__main__':
    main()
