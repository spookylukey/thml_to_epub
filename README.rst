Converter from ThML to epub
===========================

`ThML <http://www.ccel.org/ThML/>`_ is a format used to mark up theological
books, developed CCEL.

This repo contains the beginnings of a converter to epub format.

Currently, it is extremely basic, and just assumes that ThML files are
actually HTML files.  Since ThML is based on HTML and ebook readers are
based on HTML and tend to be tolerant of errors, this works surprisingly
well with some readers e.g. calibre. With others you will just get a mess.

Usage
~~~~~

    $ python thml_to_epub.py book.xml

An epub file ``book.rough.epub`` is created ('rough' to indicate the current
state of conversion, and to avoid overwriting a better epub file which might
exist!)


TODO
~~~~

Almost everything!

* Extract meta data
* Go through http://www.ccel.org/ThML/ThML1.04.htm and find everything that
  needs converting.
* Convert <note place="end|foot|margin"> into endnotes with hyperlinks to the
  original position.
* Convert scripRef
* Possibly split files into multiple files in epub structure.

See http://www.manuel-strehl.de/dev/simple_epub_ebooks_with_python.en.html
