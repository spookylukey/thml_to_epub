#!/usr/bin/env python

from collections import defaultdict
import argparse
import os.path
import re
import sys
import urllib
import uuid
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
    'style': COPY,
    'id': COPY,
    'class': COPY,
    'lang': COPY,
    'title': COPY,
    'dir': COPY,
}

# Base class
class Handler(object):
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
        def handle_node(self, converter, from_node, output_parent):
            retval = super(divhandler, self).handle_node(converter, from_node, output_parent)
            # TODO - collect info for headings/TOC
            return retval
        def post_process(self, converter, output_dom):
            pass # TODO - create TOC
    return divhandler

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
            sys.stderr.write("WARNING: can't get 'passage' from scripRef attribs {0} on line {1}\n".format(from_node.attrib, from_node.sourceline))
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
        note.sourceline = from_node.sourceline

        # Build anchor
        anchor = etree.Element("a",
                               {'href': '#' + note_id,
                                'id': self.next_anchor_id(),
                            })
        anchor.sourceline = from_node.sourceline
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
                sys.stderr.write("WARNING: Can't find a div to place footnote for note on line {0}\n".format(anchor.sourceline))
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
    LineHandler,
    ScripRefHandler,
    MAP('pb', 'br', dplus(ADEFS, {'n': REMOVE, 'href': REMOVE})),
    NoteHandler,

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

DOCTYPE = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n"""

class ThmlToHtml(object):
    def __init__(self):
        self.handlers = [cls() for cls in HANDLERS]
        self.metadata = {}

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
    def __init__(self, file_name, content, file_id):
        super(ContentFile, self).__init__(file_name, content)
        self.file_id = file_id


class ContentFileCollection(object):
    def __init__(self):
        self.files = []

    def __iter__(self):
        return iter(self.files)

    def append(self, file_name, content):
        f = ContentFile(file_name, content, "file_{0}".format(len(self.files) + 1))
        self.files.append(f)
        return f

    def __getitem__(self, idx):
        return self.files[idx]


CREATOR_ROLES = {
    'Author': 'aut',
    'Author of section': 'aut',
    'Editor': 'edt',
}

def map_creator_role(thml_creator_sub):
    # For a given DC.Creator 'sub' value used in ThML docs, return the
    # Dublin Core creator 'role' value.
    if thml_creator_sub not in CREATOR_ROLES:
        sys.stderr.write("WARNING: Unhandled DC.Creator sub value '{0}'".format(thml_creator_sub))
        return "oth"
    return CREATOR_ROLES[thml_creator_sub]

def create_epub(input_html_pairs, metadata, outputfilename):
    content_files = ContentFileCollection()
    for i, (src_name, html_data) in enumerate(input_html_pairs):
        content_files.append("OEBPS/{0}.html".format(i + 1), html_data)

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
    i = metadata['dc:identifier']
    if len(i) == 0:
        i.insert(0, (identifier_val, {'id': identifier_id}))

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
    ncx_file.content = '''<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"
                 "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{identifier_val}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle>
    <text>{title}</text>
  </docTitle>
  <navMap>
    <navPoint id="navpoint-1" playOrder="1">
      <navLabel>
        <text>{title}</text>
      </navLabel>
      <content src="{content_file}"/>
    </navPoint>
  </navMap>
</ncx>'''.format(
    identifier_val=html_escape(identifier_val),
    title=html_escape(title),
    content_file=content_files[0].get_path_relative_to_file(ncx_file)
    )
    return ncx_file


### Main ###

parser = argparse.ArgumentParser()
parser.add_argument("thml_file", nargs='+')

def main():
    args = parser.parse_args()
    input_files = args.thml_file
    outputfile = input_files[0].replace('.xml', '').replace('.thml', '') + ".rough.epub"

    input_thml_pairs = [(fn, file(fn).read()) for fn in input_files]
    converter = ThmlToHtml()
    input_html_pairs = [(fn, converter.transform(t, full_xml=True)) for fn, t in input_thml_pairs]
    create_epub(input_html_pairs, converter.metadata, outputfile)


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
""")
    assert '<title>Interesting Things</title>' in html
    assert converter.metadata['dc:title'] == [("Interesting Things", {})]
    assert converter.metadata['dc:creator'] == [("Daffy Duck", {'sub': 'Author',
                                                                'scheme': 'file-as'}),
                                                ("D. Duck", {'sub': 'Author',
                                                             'scheme': 'short-form'}),
                                                ("daffy", {'sub': 'Author',
                                                           'scheme': 'abcd'})]
if __name__ == '__main__':
    main()
