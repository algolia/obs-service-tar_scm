import datetime
import logging
import os
import shutil
import sys
import tempfile
import stat

import TarSCM.cli
from TarSCM.config import config


class Changes():
    def import_xml_parser(self):
        """Import the best XML parser available.  Currently prefers lxml and
        falls back to xml.etree.

        There are some important differences in behaviour, which also
        depend on the Python version being used:

        | Python    | 2.6            | 2.6         | 2.7            | 2.7         |
        |-----------+----------------+-------------+----------------+-------------|
        | module    | lxml.etree     | xml.etree   | lxml.etree     | xml.etree   |
        |-----------+----------------+-------------+----------------+-------------|
        | empty     | XMLSyntaxError | ExpatError  | XMLSyntaxError | ParseError  |
        | doc       | "Document is   | "no element | "Document is   | "no element |
        |           | empty"         | found"      | empty          | found"      |
        |-----------+----------------+-------------+----------------+-------------|
        | syntax    | XMLSyntaxError | ExpatError  | XMLSyntaxError | ParseError  |
        | error     | "invalid       | "not well-  | "invalid       | "not well-  |
        |           | element name"  | formed"     | element name"  | formed"     |
        |-----------+----------------+-------------+----------------+-------------|
        | e.message | deprecated     | deprecated  | yes            | yes         |
        |-----------+----------------+-------------+----------------+-------------|
        | str()     | yes            | yes         | yes            | yes         |
        |-----------+----------------+-------------+----------------+-------------|
        | @attr     | yes            | no          | yes            | yes         |
        | selection |                |             |                |             |
        """  # noqa
        global ET

        try:
            # If lxml is available, we can use a parser that doesn't
            # destroy comments
            import lxml.etree as ET
            xml_parser = ET.XMLParser(remove_comments=False)
        except ImportError:
            import xml.etree.ElementTree as ET
            xml_parser = None
            if not hasattr(ET, 'ParseError'):
                try:
                    import xml.parsers.expat
                except:
                    raise RuntimeError("Couldn't load XML parser error class")

        return xml_parser

    def parse_servicedata_xml(self, srcdir):
        """Parses the XML in _servicedata.  Returns None if the file doesn't
        exist or is empty, or the ElementTree on successful parsing, or
        raises any other exception generated by parsing.
        """
        # Even if there's no _servicedata, we'll need the module later.
        xml_parser = self.import_xml_parser()

        servicedata_file = os.path.join(srcdir, "_servicedata")
        if not os.path.exists(servicedata_file):
            return None

        try:
            return ET.parse(servicedata_file, parser=xml_parser)
        except StandardError as exc:
            # Tolerate an empty file, but any other parse error should be
            # made visible.
            if str(exc).startswith("Document is empty") or \
               str(exc).startswith("no element found"):
                return None
            raise

    def extract_tar_scm_service(self, root, url):
        """Returns an object representing the <service name="tar_scm">
        element referencing the given URL.
        """
        try:
            tar_scm_services = root.findall("service[@name='tar_scm']")
        except SyntaxError:
            raise RuntimeError(
                "Couldn't load an XML parser supporting attribute selection. "
                "Try installing lxml.")

        for service in tar_scm_services:
            for param in service.findall("param[@name='url']"):
                if param.text == url:
                    return service

    def get_changesrevision(self, tar_scm_service):
        """Returns an object representing the <param name="changesrevision">
        element, or None, if it doesn't exist.
        """
        params = tar_scm_service.findall("param[@name='changesrevision']")
        if not params:
            return None
        if len(params) > 1:
            raise RuntimeError('Found multiple <param name="changesrevision"> '
                               'elements in _servicedata.')
        return params[0]

    def read_changes_revision(self, url, srcdir, outdir):
        """
        Reads the _servicedata file and returns a dictionary with 'revision' on
        success. As a side-effect it creates the _servicedata file if it
        doesn't exist. 'revision' is None in that case.
        """
        write_servicedata = False

        xml_tree = self.parse_servicedata_xml(srcdir)
        if xml_tree is None:
            root = ET.fromstring("<servicedata>\n</servicedata>\n")
            write_servicedata = True
        else:
            root = xml_tree.getroot()

        service = self.extract_tar_scm_service(root, url)
        if service is None:
            service = ET.fromstring("""\
              <service name="tar_scm">
                <param name="url">%s</param>
              </service>
            """ % url)
            root.append(service)
            write_servicedata = True

        if write_servicedata:
            ET.ElementTree(root).write(os.path.join(outdir, "_servicedata"))
        else:
            if not os.path.exists(os.path.join(outdir, "_servicedata")) or \
               not os.path.samefile(os.path.join(srcdir, "_servicedata"),
                                    os.path.join(outdir, "_servicedata")):
                shutil.copy(os.path.join(srcdir, "_servicedata"),
                            os.path.join(outdir, "_servicedata"))

        change_data = {
            'revision': None
        }
        changesrevision_element = self.get_changesrevision(service)
        if changesrevision_element is not None:
            change_data['revision'] = changesrevision_element.text
        return change_data

    def write_changes_revision(self, url, outdir, new_revision):
        """Updates the changesrevision in the _servicedata file."""
        logging.debug("Updating %s", os.path.join(outdir, '_servicedata'))

        xml_tree = self.parse_servicedata_xml(outdir)
        root = xml_tree.getroot()
        tar_scm_service = self.extract_tar_scm_service(root, url)
        if tar_scm_service is None:
            sys.exit("File _servicedata is missing tar_scm with URL '%s'" %
                     url)

        changed = False
        element = self.get_changesrevision(tar_scm_service)
        if element is None:
            changed = True
            changesrevision = ET.fromstring(
                "    <param name=\"changesrevision\">%s</param>\n"
                % new_revision)
            tar_scm_service.append(changesrevision)
        elif element.text != new_revision:
            element.text = new_revision
            changed = True

        if changed:
            xml_tree.write(os.path.join(outdir, "_servicedata"))

    def write_changes(self, changes_filename, changes, version, author):
        """Add changes to given *.changes file."""
        if changes is None:
            return

        logging.debug("Writing changes file %s", changes_filename)

        tmp_fp = tempfile.NamedTemporaryFile(delete=False)
        os.chmod(tmp_fp.name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP |
                 stat.S_IROTH)
        tmp_fp.write('-' * 67 + '\n')
        tmp_fp.write("%s - %s\n" % (
            datetime.datetime.utcnow().strftime('%a %b %d %H:%M:%S UTC %Y'),
            author))
        tmp_fp.write('\n')
        tmp_fp.write("- Update to version %s:\n" % version)
        for line in changes:
            tmp_fp.write("  * %s\n" % line)
        tmp_fp.write('\n')

        old_fp = open(changes_filename, 'r')
        tmp_fp.write(old_fp.read())
        old_fp.close()

        tmp_fp.close()

        shutil.move(tmp_fp.name, changes_filename)

    def get_changesauthor(self, args):
        # return changesauthor if given as cli option
        if args.changesauthor:
            return args.changesauthor

        # find changesauthor in $HOME/.oscrc
        try:
            files = [[os.path.join(os.environ['HOME'], '.oscrc'), False]]
            cfg = config(files)

            changesauthor = None
            section = cfg.get('general', 'apiurl')
            if section:
                changesauthor = cfg.get(section, 'email')
        except KeyError:
            pass

        if not changesauthor:
            changesauthor = TarSCM.cli.DEFAULT_AUTHOR

        logging.debug("AUTHOR: %s", changesauthor)

        return changesauthor
