#!/usr/bin/env python

import os.path
import zipfile
import sys


def main(inputfiles):
    outputfile = inputfiles[0].replace('.xml', '').replace('.thml', '') + ".rough.epub"

    print "Creating {0}".format(outputfile)
    epub = zipfile.ZipFile(outputfile, "w", zipfile.ZIP_DEFLATED)

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
    for i, html_file in enumerate(inputfiles):
        basename = os.path.basename(html_file)
        manifest += '<item id="file_%s" href="%s" media-type="application/xhtml+xml"/>' % (
            i+1, basename)
        spine += '<itemref idref="file_%s" />' % (i+1)
        epub.write(html_file, 'OEBPS/'+basename, zipfile.ZIP_DEFLATED)

    # Finally, write the index
    epub.writestr('OEBPS/Content.opf', index_tpl % {
      'manifest': manifest,
      'spine': spine,
    })
    epub.close()


if __name__ == '__main__':
    main(sys.argv[1:])
