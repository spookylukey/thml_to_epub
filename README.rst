Converter from ThML to epub
===========================

`ThML <http://www.ccel.org/ThML/>`_ is a format used to mark up theological
books, developed CCEL.

This repo contains the beginnings of a converter to epub format.

Currently, it is extremely basic, and just assumes that ThML files are actually
HTML files. Since ThML is based on HTML and ebook viewers are based on HTML and
tend to be tolerant of errors, this works surprisingly well.

Usage
~~~~~

    $ python thml_to_epub.py book.xml

An epub file ``book.rough.epub`` is created ('rough' to indicate the current
state of conversion, and to avoid overwriting a better epub file which might
exist!)
