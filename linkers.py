#!/usr/bin/env python
# encoding: utf-8
"""
main.py

Created by Brant Faircloth on 2009-08-14.
Copyright (c) 2009 Brant Faircloth. All rights reserved.
"""

import os, sys, re, pdb, time, numpy, string, MySQLdb, ConfigParser, multiprocessing, cPickle, optparse, progress
from Bio import Seq
from Bio import pairwise2
from Bio.SeqIO import QualityIO
from Bio.Alphabet import SingleLetterAlphabet

def revComp(seq):
    '''Return reverse complement of seq'''
    bases = string.maketrans('AGCTagct','TCGAtcga')
    # translate it, reverse, return
    return seq.translate(bases)[::-1]

def revCompTags(tags):
    '''Return the reverse complements of a tag dictionary'''
    revTags = {}
    for tag in tags:
        revTags[revComp(tag)] = tags[tag]
    return revTags

def tagLibrary(mids, linkers, clust):
    '''Create a tag-library from the mids and the linkers which allows us to 
    track which organisms go with which MID+linker combo'''
    tl = {}
    for c in clust:
        m,l = c[0].replace(' ','').split(',')
        org = c[1]
        if mids[m] not in tl.keys():
            tl[mids[m]] = {linkers[l]:org}
        else:
            tl[mids[m]][linkers[l]] = org
    return tl

def allPossibleTags(mids, linkers, clust):
    at = []
    rat = []
    for c in clust:
        m,l = c[0].replace(' ','').split(',')
        # at = all tags; rat = reverse all tags
        at.append(linkers[l])
        rat.append(re.compile('%s' % linkers[l]))
        at.append(revComp(linkers[l]))
        rat.append(re.compile('%s' % revComp(linkers[l])))
    return at, rat
            
def trim(record, left=None, right=None):
    '''Trim a given sequence given left and right offsets'''
    if left and right:
        record = record[left:right]
    elif left:
        record = record[left:]
    elif right:
        record = record[:right]
    return record

def matches(tag, seq_match_span, tag_match_span, allowed_errors):
    '''Determine the gap/error counts for a particular match'''
    # deal with case where tag match might be perfect, but extremely gappy, 
    #e.g. ACGTCGTGCGGA-------------------------ATC
    if tag_match_span.count('-') > allowed_errors or seq_match_span.count('-')\
     > allowed_errors:
        return 0, 0
    else:
        #pdb.set_trace()
        seq_array, tag_array = numpy.array(list(seq_match_span)), \
        numpy.array(list(tag_match_span))
        matches = sum(seq_array == tag_array)
        error = sum(seq_array != tag_array) + (len(tag) - \
        len(tag_match_span.replace('-','')))
        # Original scoring method from 
        # http://github.com/chapmanb/bcbb/tree/master treats gaps incorrectly:
        #return sum((1 if s == tag_match_span[i] else 0) for i, s in 
        #enumerate(seq_match_span))
        return matches, error

def smithWaterman(seq, tags, allowed_errors):
    '''Smith-Waterman alignment method for aligning tags with their respective
    sequences.  Only called when regular expression matching patterns fail.  
    Borrowed & heavily modified from 
    http://github.com/chapmanb/bcbb/tree/master'''
    #if seq == 'CGAGAGATACAAAAGCAGCAGCGGAATCGATTCCGCTGCTGC':
    #    pdb.set_trace()
    high_score = {'tag':None, 'seq_match':None, 'mid_match':None, 'score':None, 
        'start':None, 'end':None, 'matches':None, 'errors':allowed_errors}
    for tag in tags:
        seq_match, tag_match, score, start, end = pairwise2.align.localms(seq, 
        tag, 5.0, -4.0, -9.0, -0.5, one_alignment_only=True)[0]
        seq_match_span, tag_match_span = seq_match[start:end], tag_match[start:end]
        match, errors = matches(tag, seq_match_span, tag_match_span, allowed_errors)
        if match >= len(tag)-allowed_errors and match > high_score['matches'] \
        and errors <= high_score['errors']:
            high_score['tag'] = tag
            high_score['seq_match'] = seq_match
            high_score['tag_match'] = tag_match
            high_score['score'] = score
            high_score['start'] = start
            high_score['end'] = end
            high_score['matches'] = match
            high_score['seq_match_span'] = seq_match_span
            high_score['errors'] = errors
    if high_score['matches']:
        return high_score['tag'], high_score['matches'], \
        high_score['seq_match'], high_score['seq_match_span'], \
        high_score['start'], high_score['end']
    else:
        return None

def qualTrimming(record, min_score=10):
    '''Remove ambiguous bases from 5' and 3' sequence ends'''
    s = str(record.seq)
    sl = list(s)
    for q in enumerate(record.letter_annotations["phred_quality"]):
        if q[1] < min_score:
            sl[q[0]] = 'N'
    s = ''.join(sl)
    # find runs of ambiguous bases at 5' and 3' ends
    left_re, right_re = re.compile('^N+'),re.compile('N+$')
    left_trim, right_trim = re.search(left_re, s), re.search(right_re, s)
    if left_trim:
        left_trim = left_trim.end()
    if right_trim:
        right_trim = right_trim.end()
    return trim(record, left_trim, right_trim)

def midTrim(record, tags, max_gap_char=22, **kwargs):
    '''Remove the MID tag from the sequence read'''
    #if record.id == 'MID_No_Error_ATACGACGTA':
    #    pdb.set_trace()
    s = str(record.seq)
    mid = leftLinker(s, tags, max_gap_char, True, fuzzy=kwargs['fuzzy'])
    if mid:
        trimmed = trim(record, mid[3])
        tag, m_type, seq_match = mid[0], mid[1], mid[4]
        return tag, trimmed, seq_match, m_type
    else:
        return None

def SWMatchPos(seq_match_span, start, stop):
    # slice faster than ''.startswith()
    if seq_match_span[0] == '-':
        start = start + seq_match_span.count('-')
    else:
        stop = stop - seq_match_span.count('-')
    return start, stop

def leftLinker(s, tags, max_gap_char, gaps=False, **kwargs):
    '''Mathing methods for left linker - regex first, followed by fuzzy (SW)
    alignment, if the option is passed'''
    for tag in tags:
        if gaps:
            r = re.compile(('^%s') % (tag))
        else:
            r = re.compile(('^[acgtnACGTN]{0,%s}%s') % (max_gap_char, tag))
        match = re.search(r, s)
        if match:
            m_type = 'regex'
            start, stop = match.start(), match.end()
            # by default, this is true
            seq_match = tag
            break
    #if s == 'ACCTCGTGCGGAATCGAGAGAGAGAGAGAGAGAGAGAGAGAGAGAGAGAGAG':
    #    pdb.set_trace()
    if not match and kwargs['fuzzy']:
        match = smithWaterman(s, tags, 1)
        # we can trim w/o regex
        if match:
            m_type = 'fuzzy'
            tag = match[0]
            seq_match = match[3]
            start, stop = SWMatchPos(match[3],match[4], match[5])
    if match:
        return tag, m_type, start, stop, seq_match
    else:
        return None

def rightLinker(s, tags, max_gap_char, gaps=False, **kwargs):
    '''Mathing methods for right linker - regex first, followed by fuzzy (SW)
    alignment, if the option is passed'''
    #if s == 'GAGAGAGAGAGAGAGAGAGAGAGAGAGAGAGAGAGAG':
    #    pdb.set_trace()
    revtags = revCompTags(tags)
    for tag in revtags:
        if gaps:
            r = re.compile(('%s$') % (tag))
        else:
            r = re.compile(('%s[acgtnACGTN]{0,%s}$') % (tag, max_gap_char))
        match = re.search(r, s)
        if match:
            m_type = 'regex'
            start, stop = match.start(), match.end()
            # by default, this is true
            seq_match = tag
            break
    if not match and kwargs['fuzzy']:
        match = smithWaterman(s, revtags, 1)
        # we can trim w/o regex
        if match:
            m_type = 'fuzzy'
            tag = match[0]
            seq_match = match[3]
            start, stop = SWMatchPos(match[3],match[4], match[5])
    if match:
        return revComp(tag), m_type, start, stop, seq_match
    else:
        return None

def linkerTrim(record, tags, max_gap_char=22, **kwargs):
    '''Use regular expression and (optionally) fuzzy string matching
    to locate and trim linkers from sequences'''
    #if record.id == 'FX5ZTWB02DOPOT':
    #    pdb.set_trace()
    m_type  = False
    s       = str(record.seq)
    left    = leftLinker(s, tags, max_gap_char=22, fuzzy=kwargs['fuzzy'])
    right   = rightLinker(s, tags, max_gap_char=22, fuzzy=kwargs['fuzzy'])
    if left and right and left[0] == right[0]:
        # we can have lots of conditional matches here
        if left[2] <= max_gap_char and right[2] >= (len(s) - (len(right[0]) +\
        max_gap_char)):
            trimmed = trim(record, left[3], right[2])
            # left and right are identical so largely pass back the left
            # info... except for m_type which can be a combination
            tag, m_type, seq_match = left[0], left[1]+'-'+right[1]+'-both', \
            left[4]
        else:
            pass
    elif left and right and left[0] != right[0]:
        # flag
        if left[2] <= max_gap_char and right[2] >= (len(s) - (len(right[0]) +\
        max_gap_char)):
            trimmed = None
            tag, m_type, seq_match = None, 'tag-mismatch', None
    elif left:
        if left[2] <= max_gap_char:
            trimmed = trim(record, left[3])
            tag, m_type, seq_match = left[0], left[1]+'-left', left[4]
        else:
            # flag
            pass
    elif right:
        if right[2] >= (len(s) - (len(right[0]) + max_gap_char)):
            trimmed = trim(record, None, right[2])
            tag, m_type, seq_match = right[0], right[1]+'-right', right[4]
        else:
            # flag
            pass
    if m_type:
        try:
            return tag, trimmed, seq_match, tags[tag], m_type
        except:
            return tag, trimmed, seq_match, None, m_type
    else:
        return None

def reverse(items):
    '''build a reverse dictionary from a list of tuples'''
    l = []
    for i in items:
        t = (i[1],i[0])
        l.append(t)
    return dict(l)

def createSeqTable(c):
    '''Create necessary tables in our database to hold the sequence and 
    tagging data'''
    # TODO:  move blob column to its own table, indexed by id
    # DONE:  move all tables to InnoDB??
    try:
        c.execute('''DROP TABLE sequence''')
    except:
        pass
    c.execute('''CREATE TABLE sequence (id INT UNSIGNED NOT NULL 
        AUTO_INCREMENT,name VARCHAR(100),mid VARCHAR(30),mid_seq VARCHAR(30),
        mid_match VARCHAR(30),mid_method VARCHAR(50),linker VARCHAR(50),
        linker_seq VARCHAR(50),linker_match VARCHAR(50),linker_method 
        VARCHAR(50),cluster VARCHAR(75),concat_seq VARCHAR(50), 
        concat_match varchar(50), concat_method VARCHAR(50),
        n_count SMALLINT UNSIGNED, untrimmed_len SMALLINT UNSIGNED, 
        seq_trimmed TEXT, trimmed_len SMALLINT UNSIGNED, record BLOB, PRIMARY
        KEY (id), INDEX sequence_cluster (cluster)) ENGINE=InnoDB''')

def createQualSeqTable(c):
    # TODO:  move blob column to its own table, indexed by id
    # DONE:  move all tables to InnoDB??
    try:
        c.execute('''DROP TABLE sequence''')
    except:
        pass
    c.execute('''CREATE TABLE sequence (id INT UNSIGNED NOT NULL 
        AUTO_INCREMENT,name VARCHAR(100), n_count SMALLINT UNSIGNED, 
        untrimmed_len MEDIUMINT UNSIGNED, seq_trimmed MEDIUMTEXT, trimmed_len 
        MEDIUMINT UNSIGNED, record MEDIUMBLOB, PRIMARY KEY (id)) ENGINE=InnoDB''')

def concatCheck(record, all_tags, all_tags_regex, reverse_linkers, **kwargs):
    '''Check screened sequence for the presence of concatemers by scanning 
    for all possible tags - after the 5' and 3' tags have been removed'''
    s = str(record.seq)
    m_type = None
    # do either/or to try and keep speed up, somewhat
    #if not kwargs['fuzzy']:
    #pdb.set_trace()
    for tag in all_tags_regex:
        match = re.search(tag, s)
        if match:
            tag = tag.pattern
            m_type = 'regex-concat'
            seq_match = tag
            break
    if not match and ['fuzzy']:
    #else:
        match = smithWaterman(s, all_tags, 1)
        # we can trim w/o regex
        if match:
            tag = match[0]
            m_type = 'fuzzy-concat'
            seq_match = match[3]
    if m_type:
        return tag, m_type, seq_match
    else:
        return None, None, None

def sequenceCount(input):
    '''Determine the number of sequence reads in the input'''
    handle = open(input, 'rU')
    lines = handle.read().count('>')
    handle.close()
    return lines
            

def qualOnlyWorker(record, qual, conf):
    # we need a separate connection for each mysql cursor or they are going
    # start going into locking hell and things will go poorly. Creating a new 
    # connection for each worker process is the easiest/laziest solution.
    # Connection pooling (DB-API) didn't work so hot, but probably because 
    # I'm slightly retarded.
    conn = MySQLdb.connect(user=conf.get('Database','USER'), 
        passwd=conf.get('Database','PASSWORD'), 
        db=conf.get('Database','DATABASE'))
    cur = conn.cursor()
    # convert low-scoring bases to 'N'
    untrimmed_len = len(record.seq)
    qual_trimmed = qualTrimming(record, qual)
    N_count = str(qual_trimmed.seq).count('N')
    record = qual_trimmed
    # pickle the sequence record, so we can store it as a BLOB in MySQL, we
    # can thus recurrect it as a sequence object when we need it next.
    record_pickle = cPickle.dumps(record,1)
    cur.execute('''INSERT INTO sequence (name, n_count, untrimmed_len, 
        seq_trimmed, trimmed_len, record) 
        VALUES (%s,%s,%s,%s,%s,%s)''', 
        (record.id, N_count, untrimmed_len, record.seq, len(record.seq), 
        record_pickle))
    #pdb.set_trace()
    cur.close()
    conn.commit()
    # keep our connection load low
    conn.close()
    return

def linkerWorker(record, qual, tags, all_tags, all_tags_regex, reverse_mid, reverse_linkers, conf):
    # we need a separate connection for each mysql cursor or they are going
    # start going into locking hell and things will go poorly. Creating a new 
    # connection for each worker process is the easiest/laziest solution.
    # Connection pooling (DB-API) didn't work so hot, but probably because 
    # I'm slightly retarded.
    conn = MySQLdb.connect(user=conf.get('Database','USER'), 
        passwd=conf.get('Database','PASSWORD'), 
        db=conf.get('Database','DATABASE'))
    cur = conn.cursor()
    # convert low-scoring bases to 'N'
    untrimmed_len = len(record.seq)
    qual_trimmed = qualTrimming(record, qual)
    N_count = str(qual_trimmed.seq).count('N')
    # search on 5' (left) end for MID
    mid = midTrim(qual_trimmed, tags, fuzzy=True)
    #TODO:  Add length parameters
    if mid:
        # if MID, search for exact matches (for and revcomp) on Linker
        # provided no exact matches, use fuzzy matching (Smith-Waterman) +
        # error correction to find Linker
        mid, trimmed, seq_match, m_type = mid
        linker = linkerTrim(trimmed, tags[mid], fuzzy=True)
        if linker:
            l_tag, l_trimmed, l_seq_match, l_critter, l_m_type = linker
        else:
            l_tag, l_trimmed, l_seq_match, l_critter, l_m_type, concat_type, \
            concat_count = (None,) * 7
    else:
        mid, trimmed, seq_match, m_type = (None,) * 4
        l_tag, l_trimmed, l_seq_match, l_critter, l_m_type, concat_type, \
        concat_count = (None,) * 7
    # check for concatemers
    concat_check = False
    if concat_check:
        if l_trimmed and len(l_trimmed.seq) > 0:
            concat_tag, concat_type, concat_seq_match = concatCheck(l_trimmed, 
                all_tags, all_tags_regex, reverse_linkers, fuzzy=True)
        else:
            concat_tag, concat_type, concat_seq_match = None, None, None
    else:
        concat_tag, concat_type, concat_seq_match = None, None, None
    # if we are able to trim the linker
    if l_trimmed:
        record = l_trimmed
    # if we are able to trim the MID
    elif trimmed:
        record = trimmed
    # pickle the sequence record, so we can store it as a BLOB in MySQL, we
    # can thus recurrect it as a sequence object when we need it next.
    record_pickle = cPickle.dumps(record,1)
    cur.execute('''INSERT INTO sequence (name, mid, mid_seq, mid_match, 
        mid_method, linker, linker_seq, linker_match, linker_method, cluster, 
        concat_seq, concat_match, concat_method, n_count, untrimmed_len, 
        seq_trimmed, trimmed_len, record) 
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''', 
        (record.id, reverse_mid[mid], mid, seq_match, m_type, 
        reverse_linkers[l_tag], l_tag, l_seq_match, l_m_type, l_critter, 
        concat_tag, concat_seq_match, concat_type, N_count, untrimmed_len,
        record.seq, len(record.seq), record_pickle))
    #pdb.set_trace()
    cur.close()
    conn.commit()
    # keep our connection load low
    conn.close()
    return

def motd():
    '''Startup info'''
    motd = '''
    ##############################################################
    #                     msatcommander 454                      #
    #                                                            #
    # - parsing and error correction for sequence tagged primers #
    # - microsatellite identification                            #
    # - sequence pooling                                         #
    # - primer design                                            #
    #                                                            #
    # Copyright (c) 2009 Brant C. Faircloth & Travis C. Glenn    #
    ##############################################################\n
    '''
    print motd

def interface():
    '''Command-line interface'''
    usage = "usage: %prog [options]"

    p = optparse.OptionParser(usage)

    p.add_option('--configuration', '-c', dest = 'conf', action='store', \
type='string', default = None, help='The path to the configuration file.', \
metavar='FILE')

    (options,arg) = p.parse_args()
    if not options.conf:
        p.print_help()
        sys.exit(2)
    if not os.path.isfile(options.conf):
        print "You must provide a valid path to the configuration file."
        p.print_help()
        sys.exit(2)
    return options, arg

def main():
    '''Main loop'''
    start_time = time.time()
    options, arg = interface()
    motd()
    print 'Started: ', time.strftime("%a %b %d, %Y  %H:%M:%S", time.localtime(start_time))
    conf = ConfigParser.ConfigParser()
    conf.read(options.conf)
    conn = MySQLdb.connect(user=conf.get('Database','USER'), 
        passwd=conf.get('Database','PASSWORD'), 
        db=conf.get('Database','DATABASE'))
    cur = conn.cursor()
    qualTrim = conf.getboolean('Steps', 'TRIM')
    qual = conf.getint('Qual', 'MIN_SCORE')
    linkerTrim = conf.getboolean('Steps', 'LINKERTRIM')
    if qualTrim and not linkerTrim:
        createQualSeqTable(cur)
        conn.commit()
    elif qualTrim and linkerTrim:
        mid, reverse_mid = dict(conf.items('MID')), reverse(conf.items('MID'))
        linkers, reverse_linkers = dict(conf.items('Linker')), reverse(conf.items('Linker'))
        #TODO:  Add levenshtein distance script to automagically determine 
        #distance
        reverse_mid[None] = None
        reverse_linkers[None] = None
        clust = conf.items('Clusters')
        # build tag library 1X
        tags = tagLibrary(mid, linkers, clust)
        all_tags, all_tags_regex = allPossibleTags(mid, linkers, clust)
        # crank out a new table for the data
        createSeqTable(cur)
        conn.commit()
    seqcount = sequenceCount(conf.get('Input','sequence'))
    record = QualityIO.PairedFastaQualIterator(
    open(conf.get('Input','sequence'), "rU"), 
    open(conf.get('Input','qual'), "rU"))
    #pdb.set_trace()
    if conf.getboolean('Multiprocessing', 'MULTIPROCESSING'):
        # get num processors
        n_procs = conf.get('Multiprocessing','processors')
        if n_procs == 'Auto':
            # TODO:  change this?
            # we'll start 2X-1 threads (X = processors).
            n_procs = multiprocessing.cpu_count() - 1
        else:
            n_procs = int(n_procs)
        print 'Multiprocessing.  Number of processors = ', n_procs
        # to test with fewer sequences
        #count = 0
        try:
            threads = []
            pb = progress.bar(0,seqcount,60)
            pb_inc = 0
            while record:
                if len(threads) < n_procs:
                    if qualTrim and not linkerTrim:
                        p = multiprocessing.Process(target=qualOnlyWorker, args=(
                        record.next(), qual, conf))
                    elif qualTrim and linkerTrim:
                        p = multiprocessing.Process(target=linkerWorker, args=(
                        record.next(), qual, tags, all_tags, all_tags_regex, 
                        reverse_mid, reverse_linkers, conf))
                    p.start()
                    threads.append(p)
                    if (pb_inc+1)%1000 == 0:
                        pb.__call__(pb_inc+1)
                    elif pb_inc + 1 == seqcount:
                        pb.__call__(pb_inc+1)
                    pb_inc += 1
                else:
                    for t in threads:
                        if not t.is_alive():
                            threads.remove(t)
        except StopIteration:
            pass
    else:
        print 'Not using multiprocessing'
        count = 0
        try:
            pb = progress.bar(0,seqcount,60)
            pb_inc = 0
            #while count < 1000:
            while record:
                #count +=1
                if qualTrim and not linkerTrim:
                    qualOnlyWorker(record.next(), qual, conf)
                elif qualTrim and linkerTrim:
                    linkerWorker(record.next(), qual, tags, all_tags, 
                    all_tags_regex, reverse_mid, reverse_linkers, conf)
                if (pb_inc+1)%1000 == 0:
                    pb.__call__(pb_inc+1)
                elif pb_inc + 1 == seqcount:
                    pb.__call__(pb_inc+1)
                pb_inc += 1
        except StopIteration:
            pass
    print '\n'
    cur.close()
    conn.close()
    end_time = time.time()
    print 'Ended: ', time.strftime("%a %b %d, %Y  %H:%M:%S", time.localtime(end_time))
    print '\nTime for execution: ', (end_time - start_time)/60, 'minutes'

if __name__ == '__main__':
    main()
