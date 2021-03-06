#!/usr/bin/python -OO
# Copyright 2008-2015 The SABnzbd-Team <team@sabnzbd.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.decoder - article decoder
"""

import Queue
import binascii
import logging
import re
from time import sleep
from threading import Thread
try:
    import _yenc
    HAVE_YENC = True

except ImportError:
    HAVE_YENC = False

import sabnzbd
from sabnzbd.constants import Status, MAX_DECODE_QUEUE
from sabnzbd.articlecache import ArticleCache
import sabnzbd.downloader
import sabnzbd.cfg as cfg
from sabnzbd.encoding import yenc_name_fixer
from sabnzbd.misc import match_str, int_conv


class CrcError(Exception):

    def __init__(self, needcrc, gotcrc, data):
        Exception.__init__(self)
        self.needcrc = needcrc
        self.gotcrc = gotcrc
        self.data = data


class BadYenc(Exception):

    def __init__(self):
        Exception.__init__(self)


class Decoder(Thread):

    def __init__(self, servers):
        Thread.__init__(self)

        self.queue = Queue.Queue()
        self.servers = servers

    def decode(self, article, lines):
        self.queue.put((article, lines))
        # See if there's space left in cache, pause otherwise
        # But do allow some articles to enter queue, in case of full cache
        if not ArticleCache.do.reserve_space(lines) and self.queue.qsize() > MAX_DECODE_QUEUE:
            sabnzbd.downloader.Downloader.do.delay()

    def stop(self):
        self.queue.put(None)

    def run(self):
        from sabnzbd.nzbqueue import NzbQueue
        while 1:
            sleep(0.001)
            art_tup = self.queue.get()
            if not art_tup:
                break

            article, lines = art_tup
            nzf = article.nzf
            nzo = nzf.nzo
            art_id = article.article
            killed = False

            # Check if the space that's now free can let us continue the queue?
            if (ArticleCache.do.free_reserve_space(lines) or self.queue.qsize() < MAX_DECODE_QUEUE) and sabnzbd.downloader.Downloader.do.delayed:
                sabnzbd.downloader.Downloader.do.undelay()

            data = None
            register = True  # Finish article
            found = False    # Proper article found
            logme = None

            if lines:
                try:
                    if nzo.precheck:
                        raise BadYenc
                    register = True
                    logging.debug("Decoding %s", art_id)

                    data = decode(article, lines)
                    nzf.article_count += 1
                    found = True
                except IOError, e:
                    logme = T('Decoding %s failed') % art_id
                    logging.warning(logme)
                    logging.info("Traceback: ", exc_info=True)

                    sabnzbd.downloader.Downloader.do.pause()

                    article.fetcher = None

                    NzbQueue.do.reset_try_lists(nzf, nzo)

                    register = False

                except CrcError, e:
                    logme = T('CRC Error in %s (%s -> %s)') % (art_id, e.needcrc, e.gotcrc)
                    logging.info(logme)

                    data = e.data

                except BadYenc:
                    # Handles precheck and badly formed articles
                    killed = False
                    found = False
                    if nzo.precheck and lines and lines[0].startswith('223 '):
                        # STAT was used, so we only get a status code
                        found = True
                    else:
                        # Examine headers (for precheck) or body (for download)
                        # And look for DMCA clues (while skipping "X-" headers)
                        for line in lines:
                            lline = line.lower()
                            if 'message-id:' in lline:
                                found = True
                            if not line.startswith('X-') and match_str(lline, ('dmca', 'removed', 'cancel', 'blocked')):
                                killed = True
                                break
                    if killed:
                        logme = 'Article removed from server (%s)'
                        logging.info(logme, art_id)
                    if nzo.precheck:
                        if found and not killed:
                            # Pre-check, proper article found, just register
                            logging.debug('Server has article %s', art_id)
                            register = True
                    elif not killed and not found:
                        logme = T('Badly formed yEnc article in %s') % art_id
                        logging.info(logme)

                    if not found or killed:
                        new_server_found = self.__search_new_server(article)
                        if new_server_found:
                            register = False
                            logme = None

                except:
                    logme = T('Unknown Error while decoding %s') % art_id
                    logging.info(logme)
                    logging.info("Traceback: ", exc_info=True)

                    new_server_found = self.__search_new_server(article)
                    if new_server_found:
                        register = False
                        logme = None

                if logme:
                    if killed:
                        nzo.inc_log('killed_art_log', art_id)
                    else:
                        nzo.inc_log('bad_art_log', art_id)

            else:
                new_server_found = self.__search_new_server(article)
                if new_server_found:
                    register = False
                elif nzo.precheck:
                    found = False

            if logme or not found:
                # Add extra parfiles when there was a damaged article
                if cfg.prospective_par_download() and nzo.extrapars:
                    # How many do we need?
                    bad = len(nzo.nzo_info.get('bad_art_log', []))
                    miss = len(nzo.nzo_info.get('missing_art_log', []))
                    killed = len(nzo.nzo_info.get('killed_art_log', []))
                    dups = len(nzo.nzo_info.get('dup_art_log', []))
                    total_need = bad + miss + killed + dups

                    # How many do we already have?
                    blocks_already = 0
                    for nzf in nzo.files:
                        # Only par2 files have a blocks attribute
                        if nzf.blocks:
                            blocks_already = blocks_already + int_conv(nzf.blocks)

                    # Need more?
                    if blocks_already < total_need:
                        # We have to find the right par-set
                        for parset in nzo.extrapars.keys():
                            if parset in nzf.filename and nzo.extrapars[parset]:
                                extrapars_sorted = sorted(nzo.extrapars[parset], key=lambda x: x.blocks, reverse=True)
                                # Loop untill we have enough
                                while blocks_already < total_need and extrapars_sorted:
                                    # Add the first one
                                    new_nzf = extrapars_sorted.pop()
                                    nzo.add_parfile(new_nzf)
                                    nzo.extrapars[parset] = extrapars_sorted
                                    blocks_already = blocks_already + int_conv(new_nzf.blocks)
                                    logging.info('Prospectively added %s repair blocks to %s', new_nzf.blocks, nzo.final_name)

            if data:
                ArticleCache.do.save_article(article, data)

            if register:
                NzbQueue.do.register_article(article, found)

    def __search_new_server(self, article):
        from sabnzbd.nzbqueue import NzbQueue
        article.add_to_try_list(article.fetcher)

        nzf = article.nzf
        nzo = nzf.nzo

        new_server_found = False
        fill_server_found = False

        for server in self.servers:
            if server.active and not article.server_in_try_list(server):
                if not sabnzbd.highest_server(server):
                    fill_server_found = True
                else:
                    new_server_found = True
                    break

        # Only found one (or more) fill server(s)
        if not new_server_found and fill_server_found:
            article.allow_fill_server = True
            new_server_found = True

        if new_server_found:
            article.fetcher = None
            article.tries = 0

            # Allow all servers to iterate over this nzo and nzf again
            NzbQueue.do.reset_try_lists(nzf, nzo)

            if sabnzbd.LOG_ALL:
                logging.debug('%s => found at least one untested server', article)

        else:
            msg = T('%s => missing from all servers, discarding') % article
            logging.info(msg)
            article.nzf.nzo.inc_log('missing_art_log', msg)

        return new_server_found


YDEC_TRANS = ''.join([chr((i + 256 - 42) % 256) for i in xrange(256)])
def decode(article, data):
    # Filter out empty ones
    data = filter(None, data)
    # No point in continuing if we don't have any data left
    if data:
        nzf = article.nzf
        yenc, data = yCheck(data)
        ybegin, ypart, yend = yenc
        decoded_data = None

        # Deal with non-yencoded posts
        if not ybegin:
            found = False
            try:
                for i in xrange(min(40, len(data))):
                    if data[i].startswith('begin '):
                        nzf.type = 'uu'
                        found = True
                        # Pause the job and show warning
                        if nzf.nzo.status != Status.PAUSED:
                            nzf.nzo.pause()
                            msg = T('UUencode detected, only yEnc encoding is supported [%s]') % nzf.nzo.final_name
                            logging.warning(msg)
                        break
            except IndexError:
                raise BadYenc()

            if found:
                decoded_data = ''
            else:
                raise BadYenc()

        # Deal with yenc encoded posts
        elif (ybegin and yend):
            if 'name' in ybegin:
                nzf.filename = yenc_name_fixer(ybegin['name'])
            else:
                logging.debug("Possible corrupt header detected => ybegin: %s", ybegin)
            nzf.type = 'yenc'
            # Decode data
            if HAVE_YENC:
                decoded_data, crc = _yenc.decode_string(''.join(data))[:2]
                partcrc = '%08X' % ((crc ^ -1) & 2 ** 32L - 1)
            else:
                data = ''.join(data)
                for i in (0, 9, 10, 13, 27, 32, 46, 61):
                    j = '=%c' % (i + 64)
                    data = data.replace(j, chr(i))
                decoded_data = data.translate(YDEC_TRANS)
                crc = binascii.crc32(decoded_data)
                partcrc = '%08X' % (crc & 2 ** 32L - 1)

            if ypart:
                crcname = 'pcrc32'
            else:
                crcname = 'crc32'

            if crcname in yend:
                _partcrc = '0' * (8 - len(yend[crcname])) + yend[crcname].upper()
            else:
                _partcrc = None
                logging.debug("Corrupt header detected => yend: %s", yend)

            if not (_partcrc == partcrc):
                raise CrcError(_partcrc, partcrc, decoded_data)
        else:
            raise BadYenc()

        return decoded_data


def yCheck(data):
    ybegin = None
    ypart = None
    yend = None

    # Check head
    for i in xrange(min(40, len(data))):
        try:
            if data[i].startswith('=ybegin '):
                splits = 3
                if data[i].find(' part=') > 0:
                    splits += 1
                if data[i].find(' total=') > 0:
                    splits += 1

                ybegin = ySplit(data[i], splits)

                if data[i + 1].startswith('=ypart '):
                    ypart = ySplit(data[i + 1])
                    data = data[i + 2:]
                    break
                else:
                    data = data[i + 1:]
                    break
        except IndexError:
            break

    # Check tail
    for i in xrange(-1, -11, -1):
        try:
            if data[i].startswith('=yend '):
                yend = ySplit(data[i])
                data = data[:i]
                break
        except IndexError:
            break

    return ((ybegin, ypart, yend), data)

# Example: =ybegin part=1 line=128 size=123 name=-=DUMMY=- abc.par
YSPLIT_RE = re.compile(r'([a-zA-Z0-9]+)=')
def ySplit(line, splits=None):
    fields = {}

    if splits:
        parts = YSPLIT_RE.split(line, splits)[1:]
    else:
        parts = YSPLIT_RE.split(line)[1:]

    if len(parts) % 2:
        return fields

    for i in range(0, len(parts), 2):
        key, value = parts[i], parts[i + 1]
        fields[key] = value.strip()

    return fields
