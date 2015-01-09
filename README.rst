Converter from ThML to epub
===========================

`ThML <http://www.ccel.org/ThML/>`_ is a format used to mark up theological
books, developed by CCEL.

This repo contains the beginnings of a converter to epub format.

It doesn't yet produce fully valid epub files, but they can be viewed in several
ebook readers including calibre and lucidor.


Usage
~~~~~

    $ python thml_to_epub.py book.xml

An epub file ``book.rough.epub`` is created ('rough' to indicate the current
state of conversion, and to avoid overwriting a better epub file which might
exist!)


TODO
~~~~

* Extract meta data and add to epub file
* Go through http://www.ccel.org/ThML/ThML1.04.htm and find everything that
  needs converting.
* Convert <note place="end|foot|margin"> into endnotes with hyperlinks to the
  original position.
* Possibly split files into multiple files in epub structure.
* Build table of contents

See http://www.manuel-strehl.de/dev/simple_epub_ebooks_with_python.en.html
