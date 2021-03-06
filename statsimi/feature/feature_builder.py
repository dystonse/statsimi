# -*- coding: utf-8 -*-
'''
Copyright 2019, University of Freiburg.
Chair of Algorithms and Data Structures.
'''

# dont use X backend for matplotlib rendering
import string
import os
import copy
import logging
from statistics import mean
from statistics import median
from numpy import argsort
import random
from scipy.sparse import csr_matrix
from numpy import uint8
from statsimi.feature.station_idx import StationIdx
from statsimi.util import hav
from statsimi.util import centroid
from statsimi.util import ed
from statsimi.util import bts_simi
from statsimi.util import jaro_simi
from statsimi.util import jaro_winkler_simi
from statsimi.util import ped
from statsimi.util import sed
from statsimi.util import jaccard
from statsimi.util import FileList
from statsimi.util import hav_approx
from statsimi.util import hav_approx_poly_poly
from statsimi.util import hav_approx_poly_stat
import matplotlib.pyplot as plt


class FeatureBuilder(object):
    '''
    Builds a feature matrix out of a list of station groups and stations.
    '''

    def __init__(
        self,
        bbox,
        ngram_idx=None,
        force_orphans=False,
        spice=0,
        topk=250,
        num_pos_pairs=2,
        ngram=3,
        cutoffdist=1000,
        features=['lev_simi', 'geodist'],
        pairsfile=None,
        clean_data=False
    ):

        # list of arguments needed to later init a matching feature builder
        self.initargs = {
            "topk": topk,
            "cutoffdist": cutoffdist,
            "num_pos_pairs": 2,
            "ngram": 3,
            "features": features.copy()
        }

        self.log = logging.getLogger('featbld')

        if force_orphans:
            self.log.info("(forcing pairs for station orphans)")

        self._grps = []
        self._stats = []
        self._pairs = []
        self.clean_data = clean_data

        self._spice_off = 0

        self._pairsfile = None
        if pairsfile is not None:
            self._pairsfile = open(pairsfile, 'w')

        self.dists = []
        self.dists_comp = []

        # a high number of pos pairs may lead to local overfitting
        self.num_pos_pairs = num_pos_pairs

        self.force_orphans = force_orphans

        self.spice = spice

        # maps ngrams to their ids
        self.ngram_id_idx = {}

        # list of ngrams
        self.id_ngram_idx = []

        # top ngrams
        self.top_ngrams = None

        self.topngram_idx = []
        self.st_ngram_idx = []
        self.st_ngram_idx_set = []
        self.top_ngrams_map = []

        self.reuse_ngram_idx = False

        if ngram_idx:
            self.log.info("Using giving ngrams from existing model...")
            self.reuse_ngram_idx = True
            self.ngram_id_idx = ngram_idx[0].copy()
            self.id_ngram_idx = ngram_idx[1].copy()
            self.top_ngrams = ngram_idx[2].copy()

        self.topk = topk

        self.features = features
        self.num_feats = 0
        self.feature_idx = {}

        self.bbox = bbox
        self.ngram = ngram

        # The cutoff distance is the distance in meters at which we say that
        # two stations are certainly not equivalent
        self.cutoff = cutoffdist

        self.lev_simi_idx = None
        self.geodist_idx = None
        self.ped_simi_fw_idx = None
        self.ped_simi_bw_idx = None
        self.sed_simi_fw_idx = None
        self.sed_simi_bw_idx = None
        self.jaccard_simi_idx = None
        self.missing_ngram_count_idx = None
        self.bts_simi_idx = None
        self.jaro_simi_idx = None
        self.jaro_winkler_simi_idx = None

        self.matrix = csr_matrix(([], [], [0]), shape=(
            0, self.num_feats + 1 + self.topk),
            dtype=uint8)

        self.prepare_features()

    def get_feat_idx(self, feat):
        return self.feature_idx[feat]

    def get_ngram_idx(self):
        return [self.ngram_id_idx, self.id_ngram_idx, self.top_ngrams]

    @property
    def stations(self):
        return self._stats

    @property
    def pairs(self):
        return self._pairs

    @property
    def groups(self):
        return self._grps

    def print_eq_dist_histo(self):
        if len(self.dists) == 0:
            return
        w = 10
        scale = 0.35
        plt.rcParams["figure.figsize"] = [16 * scale, 6 * scale]
        plt.hist(self.dists, rwidth=0.5, edgecolor='black', linewidth=0.7,
                 bins=range(min(self.dists), max(self.dists), w), align='mid')

        from matplotlib import ticker
        formatter = ticker.ScalarFormatter(useMathText=True)
        formatter.set_scientific(True)
        formatter.set_powerlimits((-1, 1))

        ax = plt.subplot(111)
        # plt.xticks(range(min(self.dists), max(self.dists), w))
        ax.spines['right'].set_visible(False)
        ax.spines['top'].set_visible(False)
        ax.set_xlim(-2, None)

        ax.yaxis.set_major_formatter(formatter)

        ax.get_xaxis().tick_bottom()
        ax.get_yaxis().tick_left()

        ax.set_xlabel('Distance in meters')
        ax.set_ylabel('Number of similar pairs')

        plt.savefig(os.path.join('dist_histo.pgf'),
                    bbox_inches='tight', pad_inches=0)
        plt.savefig(os.path.join('dist_histo.pdf'),
                    bbox_inches='tight', pad_inches=0)

    def print_top_k(self):
        print("== TOP " + str(len(self.get_top_ngrams())) + " n-Grams ==\n")
        for i, ngram in enumerate(self.get_top_ngrams()):
            print(str(i + 1).rjust(4, ' ') + ': ' + ngram)

    def build_from_stat_grp(self, stations, groups):
        self._grps = groups
        self._stats = stations

        self.topngram_idx = [[] for i in range(len(self._stats))]
        self.st_ngram_idx = [[] for i in range(len(self._stats))]
        self.st_ngram_idx_set = [set() for i in range(len(self._stats))]

        self.build_ngrams()
        self.build_matrix()

    def build_from_pairs(self, stations, pairs, simi):
        self._stats = stations

        self.topngram_idx = [[] for i in range(len(self._stats))]
        self.st_ngram_idx = [[] for i in range(len(self._stats))]
        self.st_ngram_idx_set = [set() for i in range(len(self._stats))]

        self.build_ngrams()

        ind, data, iptr = self.prep_matr()

        for i, pair in enumerate(pairs):
            sid1 = pair[0]
            sid2 = pair[1]
            st1 = self._stats[sid1]
            st2 = self._stats[sid2]
            matched = simi[i]
            self.write_row(sid1, sid2, st1, st2, matched, data, ind, iptr)

        self.matrix = csr_matrix(
            (data.get_mmap(), ind.get_mmap(), iptr.get_mmap()),
            shape=(len(iptr) - 1, self.num_feats + 1 + len(self.top_ngrams)),
            dtype=uint8)

    def ngrams(self, string, n):
        '''
        Return the padded n-grams for the input string

        >>> fb = FeatureBuilder([])
        >>> fb.ngrams("Lorem ipsum", 3)
        ... # doctest: +NORMALIZE_WHITESPACE
        [' Lo', 'Lor', 'ore', 'rem', 'em ', 'm i', ' ip',
        'ips', 'psu', 'sum', 'um ']
        >>> fb.ngrams("Lorem ipsum", 2)
        ... # doctest: +NORMALIZE_WHITESPACE
        [' L', 'Lo', 'or', 're', 'em', 'm ', ' i', 'ip', 'ps', 'su',
        'um', 'm ']
        >>> fb.ngrams("Lorem ipsum", 1)
        ... # doctest: +NORMALIZE_WHITESPACE
        [' ', 'L', 'o', 'r', 'e', 'm', ' ', 'i', 'p', 's', 'u', 'm', ' ']
        >>> fb.ngrams("Lorem ipsum", 13)
        ... # doctest: +NORMALIZE_WHITESPACE
        [' Lorem ipsum ']
        '''
        # we pad with ' ' to make sure that, for example, "S Bahn Freiburg"
        # and "Freiburg S Bahn" both have the ngram " S "
        string = ' ' + string + ' '
        return [string[i:i + n] for i in range(len(string) - n + 1)]

    def store_ngrams_for(self, sid):
        '''
        Store the n-grams for a particular string
        '''
        stat = self._stats[sid]
        for ng in self.ngrams(stat.name, self.ngram):
            if ng not in self.ngram_id_idx:
                self.ngram_id_idx[ng] = len(self.id_ngram_idx)
                self.st_ngram_idx[sid].append(len(self.id_ngram_idx))

                self.id_ngram_idx.append((ng, 1))
            else:
                ngid = self.ngram_id_idx[ng]
                self.st_ngram_idx[sid].append(ngid)
                self.id_ngram_idx[ngid] = (
                    self.id_ngram_idx[ngid][0],
                    self.id_ngram_idx[ngid][1] + 1)
        self.st_ngram_idx[sid] = sorted(self.st_ngram_idx[sid])
        self.st_ngram_idx_set[sid] = set(self.st_ngram_idx[sid])

    def get_top_ngrams_for(self, station):
        '''
        Get the IDs of the top n-grams for a station
        '''
        ngrams = self.ngrams(station.name, self.ngram)
        tmp = {}
        for gram in ngrams:
            if gram not in self.ngram_id_idx or \
                    self.ngram_id_idx[gram] not in self.top_ngrams:
                continue

            if gram not in tmp:
                gr_id = self.top_ngrams[self.ngram_id_idx[gram]]
                tmp[gram] = (gr_id, 1)
            else:
                tmp[gram] = (tmp[gram][0], tmp[gram][1] + 1)

        return (sorted(tmp.values()))

    def build_ngrams(self):
        '''
        Build all n-grams
        >>> from statsimi.osm.osm_parser import OsmParser
        >>> p = OsmParser()
        >>> p.parse("testdata/test.osm")
        >>> fb = FeatureBuilder(bbox=p.bounds)
        >>> fb.build_from_stat_grp(p.stations, p.groups)
        >>> fb.get_top_ngrams()
        ... # doctest: +NORMALIZE_WHITESPACE
        [' Sc', 'Sch', 'chw', 'hwa', 'wab', 'abe', 'ben', 'ent', 'nto', 'tor',
        'orb', 'rbr', 'brü', 'rüc', 'ück', 'cke', 'ke ', 'rei', 'ke,', 'e, ',
        ', F', ' Fr', 'Fre', 'eib', 'ibu', 'bur', 'urg', 'rg ', 'g i', ' im',
        'im ', 'm B', ' Br', 'Bre', 'eis', 'isg', 'sga', 'gau', 'au ']

        '''

        # if we are not re-using, store the ngrams first
        if not self.reuse_ngram_idx:
            for sid in range(len(self._stats)):
                self.store_ngrams_for(sid)

        # a is a list [(grid, (gram, occs), ..], sorted asc by occs
        a = sorted(enumerate(
            self.id_ngram_idx), key=lambda x: x[1][1], reverse=True)

        # topsorted_occ are the top k gram ids, sorted asc by occs
        topsorted_occ = [x[0] for x in a[:self.topk]]

        # topsorted_id are the top k gram ids, but sorted by their
        # gram id for faster intersection later on
        topsorted_id = sorted(topsorted_occ)

        # for each index in topsorted_id, self.top_ngrams_map stores
        # the rank of the id
        self.top_ngrams_map = argsort(topsorted_occ)

        self.top_ngrams = {y: x for x, y in enumerate(topsorted_id)}

        # if we are re-using, store the ngrams now
        if self.reuse_ngram_idx:
            for sid in range(len(self._stats)):
                self.store_ngrams_for(sid)

        for sid, st in enumerate(self._stats):
            self.topngram_idx[sid] = self.get_top_ngrams_for(st)

    def prep_matr(self):
        # random file name
        rand = ''.join(random.choice(string.ascii_lowercase +
                                     string.digits) for _ in range(8))

        # the matrix is stored on the hard disk to safe memory
        # FileList is the backing array
        ind = FileList(16, ".indices" + rand)
        data = FileList(8, ".data" + rand)
        iptr = FileList(32, ".indptr" + rand)
        iptr.append(0)

        return ind, data, iptr

    def build_matrix(self):
        '''
        Build the feature matrix
        >>> from statsimi.osm.osm_parser import OsmParser
        >>> p = OsmParser()
        >>> p.parse("testdata/test.osm")
        >>> fb = FeatureBuilder(bbox=p.bounds)
        >>> fb.build_from_stat_grp(p.stations, p.groups)
        >>> fb.get_matrix().shape
        (172, 46)
        '''

        ind, data, iptr = self.prep_matr()

        sidx = StationIdx(1000, self.bbox)
        matched = [set() for i in range(len(self._stats))]

        for stat in self._stats:
            if stat.lon == None:
                sidx.add_stat_group_poly(stat.gid, stat.poly)
            else:
                sidx.add_stat_group(stat.gid, stat.lon, stat.lat)

        self.log.info("Writing matrix from %s groups, %s station identifiers"
                      % (len(self._grps), len(self._stats)))
        self.log.info(" (using features %s)" % ', '.join(self.features))

        group_nums_aggr = 0
        group_num = 0

        for gid1, group1 in enumerate(self._grps):
            # this number excludes spice stations
            stations_in_group = 0
            for id1, sid1 in enumerate(group1.stats):
                st1 = self._stats[sid1]
                # self matches, we also always write them for orphan groups
                for id2, sid2 in enumerate(group1.stats[id1:]):
                    st2 = self._stats[sid2]
                    if len(st1.name) == 0 or len(st2.name) == 0:
                        continue
                    if sid1 != sid2:
                        d = self.dist(st1, st2)
                        if d <= 250:
                            self.dists.append(d)
                        self.dists_comp.append(d)
                    self.write_row(sid1, sid2, st1, st2, True, data, ind, iptr)

                if not group1.osm_rel_id and not self.force_orphans:
                    # dont negative-match orphan groups with any other group,
                    # as this is a frequent mistake in OSM and would undermine
                    # the ground truth
                    continue

                stations_in_group += 1

                if st1.lon == None:
                    loc = sidx.get_neighbors_poly(st1.poly, self.cutoff)
                else:
                    loc = sidx.get_neighbors(st1.lon, st1.lat, self.cutoff)

                self.build_pairs(sid1, False, loc, matched, data, ind, iptr)

                # spice with probability p
                if self.spice > 0 and random.uniform(0, 1) <= self.spice:
                    n = 5
                    # spice with N non-equal stations whose position is set
                    # near the station to give the model the chance to learn
                    # obvious mistakes

                    if st1.lon == None:
                        sp = sidx.get_neighbors_poly(st1.poly, self.cutoff * 10)
                    else:
                        sp = sidx.get_neighbors(st1.lon, st1.lat, self.cutoff * 10)

                    sp = random.sample(sp, k=min(len(sp), n))

                    self.build_pairs(sid1, True, sp, matched, data, ind, iptr)

            if stations_in_group > 1:
                group_nums_aggr += stations_in_group
                group_num += 1

        self.log.info("Average distance between matching pairs is %.2f"
                      % mean(self.dists_comp))

        self.log.info("Median distance between matching pairs is %.2f"
                      % median(self.dists))

        self.log.info("Average number of station identifiers per group is %.2f"
                      % (group_nums_aggr / group_num))

        # print histogram of distance between similar stations
        # self.print_eq_dist_histo()

        self.matrix = csr_matrix(
            (data.get_mmap(), ind.get_mmap(), iptr.get_mmap()),
            shape=(len(iptr) - 1, self.num_feats + 1 + len(self.top_ngrams)),
            dtype=uint8)

    def write_row(self, sid1, sid2, st1, st2, match, data, ind, iptr):
        '''
        Write a single row to the matrix.
        '''
        if (len(iptr) - 1) % 50000 == 1:
            self.log.info("@ pair #%d" % (len(iptr) - 1))

        if self.lev_simi_idx is not None:
            lev_simi = int((1.0 - (ed(st1.name, st2.name) / max(
                len(st1.name), len(st2.name)))) * 255)
            lev_simi = self.oflow(lev_simi, st1, st2, 255, "editdist")

            if lev_simi > 0:
                ind.append(self.lev_simi_idx)
                data.append(lev_simi)

        if self.geodist_idx is not None:
            geodist = self.dist(st1, st2) // 4
            geodist = self.oflow(geodist, st1, st2, 255, "meterdist")

            if geodist > 0:
                ind.append(self.geodist_idx)
                data.append(geodist)

        if self.ped_simi_fw_idx is not None:
            p = int((1.0 - (ped(st1.name, st2.name) / len(st1.name))) * 255)
            ped_simi_fw = self.oflow(p, st1, st2, 255, "ped_simi_fw")

            if ped_simi_fw > 0:
                ind.append(self.ped_simi_fw_idx)
                data.append(ped_simi_fw)

        if self.ped_simi_bw_idx is not None:
            p = int((1.0 - (ped(st2.name, st1.name) / len(st2.name))) * 255)
            ped_simi_bw = self.oflow(p, st1, st2, 255, "ped_simi_bw")

            if ped_simi_bw > 0:
                ind.append(self.ped_simi_bw_idx)
                data.append(ped_simi_bw)

        if self.sed_simi_fw_idx is not None:
            p = int((1.0 - (sed(st1.name, st2.name) / len(st1.name))) * 255)
            sed_simi_fw = self.oflow(p, st1, st2, 255, "sed_simi_fw")

            if sed_simi_fw > 0:
                ind.append(self.sed_simi_fw_idx)
                data.append(sed_simi_fw)

        if self.sed_simi_bw_idx is not None:
            p = int((1.0 - (sed(st2.name, st1.name) / len(st2.name))) * 255)
            sed_simi_bw = self.oflow(p, st1, st2, 255, "sed_simi_bw")

            if sed_simi_bw > 0:
                ind.append(self.sed_simi_bw_idx)
                data.append(sed_simi_bw)

        if self.jaccard_simi_idx is not None:
            j = int(jaccard(st2.name, st1.name) * 255)
            jaccard_simi = self.oflow(j, st1, st2, 255, "jaccard_simi")

            if jaccard_simi > 0:
                ind.append(self.jaccard_simi_idx)
                data.append(jaccard_simi)

        if self.bts_simi_idx is not None:
            b = int(bts_simi(st2.name, st1.name) * 255)
            bts_simi_val = self.oflow(b, st1, st2, 255, "bts")

            if bts_simi_val > 0:
                ind.append(self.bts_simi_idx)
                data.append(bts_simi_val)

        if self.jaro_simi_idx is not None:
            j = int(jaro_simi(st2.name, st1.name) * 255)
            jaro_simi_val = self.oflow(j, st1, st2, 255, "jaro")

            if jaro_simi_val > 0:
                ind.append(self.jaro_simi_idx)
                data.append(jaro_simi_val)

        if self.jaro_winkler_simi_idx is not None:
            j = int(jaro_winkler_simi(st2.name, st1.name) * 255)
            jaro_winkler_simi_val = self.oflow(j, st1, st2, 255, "jaro")

            if jaro_winkler_simi_val > 0:
                ind.append(self.jaro_winkler_simi_idx)
                data.append(jaro_winkler_simi_val)

        if self.missing_ngram_count_idx is not None:
            st1set = None
            st2set = None

            if sid1 is not None:
                st1set = self.st_ngram_idx_set[sid1]
            else:
                # in case we have no qgram index, this is a
                # set of strings
                st1set = set(self.ngrams(st1.name, self.ngram))

            if sid2 is not None:
                st2set = self.st_ngram_idx_set[sid2]
            else:
                # in case we have no qgram index, this is a
                # set of strings
                st2set = set(self.ngrams(st2.name, self.ngram))

            a = len(st1set | st2set)
            b = len(st1set & st2set)

            missing = self.oflow(a - b, st1, st2, 255, "missing_ngram_count")

            if missing > 0:
                ind.append(self.missing_ngram_count_idx)
                data.append(missing)

        pos_pairs = self.pos_pairs(st1, st2, self.num_pos_pairs)
        for id, pair in enumerate(pos_pairs):
            if pair[0] != 0:
                ind.append(
                    (self.num_feats - 2 * self.num_pos_pairs) + (2 * id))
                data.append(pair[0])

            if pair[1] != 0:
                ind.append(
                    (self.num_feats - 2 * self.num_pos_pairs) + (2 * id) + 1)
                data.append(pair[1])

        if sid1 is not None:
            topgramss1 = self.topngram_idx[sid1]
        else:
            topgramss1 = self.get_top_ngrams_for(st1)

        if sid2 is not None:
            topgramss2 = self.topngram_idx[sid2]
        else:
            topgramss2 = self.get_top_ngrams_for(st2)

        merged = self.diffmerge(topgramss1, topgramss2)

        for id, diff in merged:
            diff = self.oflow(
                abs(diff), st1, st2, 255, "qgram difference count")

            if diff > 0:
                ind.append(self.num_feats +
                           int(self.top_ngrams_map[id]))
                data.append(diff)

        if match:
            ind.append(self.num_feats + len(self.top_ngrams))
            data.append(1)

        iptr.append(len(ind))

        if sid1 is not None and sid2 is not None:
            # write pair to store
            self._pairs.append((sid1, sid2))

            if self._pairsfile is not None:
                # write pair to pairs file
                wsid1 = sid1
                wsid2 = sid2

                if st1.spice_id is not None:
                    wsid1 = st1.spice_id
                if st2.spice_id is not None:
                    wsid2 = st2.spice_id

                if st1.lat == None:
                    self.log.warn("TODO: Cannot write polygons to station file")
                else:
                    d = '\t'.join([str(wsid1), st1.name, str(st1.lat),
                                   str(st1.lon), str(wsid2), st2.name,
                                   str(st2.lat), str(st2.lon), str(int(match))])
                    self._pairsfile.write(d + '\n')

    def get_feature_vec(self, st1, st2):
        data = []
        ind = []
        iptr = [0]
        self.write_row(None, None, st1, st2, False, data, ind, iptr)
        return csr_matrix(((data, ind, iptr)), shape=(
            1, self.num_feats + len(self.top_ngrams)), dtype=uint8).todense()

    def get_matrix(self):
        return self.matrix

    def dist(self, s1, s2):
        if s1.lat != None and s2.lat != None:
            if self.cutoff < 500000:
                mdist = int(1000 * hav_approx(s1.lon, s1.lat, s2.lon, s2.lat))
            else:
                mdist = int(1000 * hav(s1.lon, s1.lat, s2.lon, s2.lat))
        elif s1.lat != None:
            mdist = int(1000 * hav_approx_poly_stat(s2.poly, s1.lon, s1.lat))
        elif s2.lat != None:
            mdist = int(1000 * hav_approx_poly_stat(s1.poly, s2.lon, s2.lat))
        else:
            mdist = int(1000 * hav_approx_poly_poly(s1.poly, s2.poly))
        return mdist

    def diffmerge(self, l1, l2):
        i = 0
        j = 0
        ret = []

        while i < len(l1) and j < len(l2):
            if l1[i][0] == l2[j][0]:
                ret.append((l1[i][0], l1[i][1] - l2[j][1]))
                i += 1
                j += 1
            elif l1[i][0] < l2[j][0]:
                ret.append(l1[i])
                i += 1
            else:
                ret.append(l2[j])
                j += 1

        while i < len(l1):
            ret.append(l1[i])
            i += 1

        while j < len(l2):
            ret.append(l2[j])
            j += 1

        return ret

    def get_top_ngrams(self):
        ret = []
        a = sorted(enumerate(self.id_ngram_idx),
                   key=lambda x: x[1][1], reverse=True)
        for rank, id in enumerate([x[0] for x in a[:self.topk]]):
            gram = self.id_ngram_idx[id]
            ret.append(gram[0])

        return ret

    def pos_pairs(self, st1, st2, n):
        if n == 0:
            return []

        pairs = [None] * n
        numtiles = 256

        if st1.lon != None:
            lon1 = st1.lon
            lat1 = st1.lat
        else:
            c = centroid(st1.poly)
            lon1 = c[0]
            lat1 = c[1]

        if st2.lon != None:
            lon2 = st2.lon
            lat2 = st2.lat
        else:
            c = centroid(st2.poly)
            lon2 = c[0]
            lat2 = c[1]

        lon = ((lon1 + lon2) / 2) + 180
        lat = ((lat1 + lat2) / 2) + 90

        tilelength_x = 360 / numtiles
        tilelength_y = 180 / numtiles

        orig_x = int((lon) / tilelength_x)
        orig_y = int((lat) / tilelength_y)

        pairs[0] = (orig_x, orig_y)

        for i in range(n - 1):
            x = int((lon - (i + 1) * (tilelength_x / n)) / tilelength_x)
            y = int((lat - (i + 1) * (tilelength_y / n)) / tilelength_y)

            pairs[i + 1] = (x, y)

        return pairs

    def oflow(self, val, st1, st2, cutoff, msg):
        if val > cutoff:
            self.log.warn("Warning: %s=%d between stations '%s' and '%s' does "
                          "not fit in 8 bit integer!" % (msg, val, st1, st2))
            val = 255
        return val

    def build_pairs(self, sid1, wiggle, groups, matched, data, ind, iptr):
        st1 = self._stats[sid1]
        group1 = self._grps[st1.gid]
        count = 0
        for gid2 in groups:
            group2 = self._grps[gid2]
            if st1.gid == gid2:
                continue

            if len(st1.name) == 0:
                continue

            if not group2.osm_rel_id and not self.force_orphans:
                # dont negative-match orphan groups with any other group, as
                # this is a frequent mistake in OSM and would undermine
                # the ground truth
                continue

            if self.clean_data and group1.osm_meta_rel_id is not None \
                    and group2.osm_meta_rel_id is not None \
                    and group1.osm_meta_rel_id == group2.osm_meta_rel_id:
                # dont negative-match groups that share a common meta group
                # these will be positive-matched to stations in their non-meta
                # group, but when they are grouped with a station in a
                # meta-group, we count their similarity as 'non decidable'
                # and keep it out of the ground truth
                continue

            inserted = False
            for sid2 in group2.stats:
                if wiggle:
                    st2 = copy.copy(self._stats[sid2])
                    st2.lon = st1.lon + random.gauss(0, 0.0005)
                    st2.lat = st1.lat + random.gauss(0, 0.0005)
                    st2.spice_id = len(self._stats) + self._spice_off
                    self._spice_off += 1
                else:
                    st2 = self._stats[sid2]

                if len(st2.name) == 0:
                    continue

                d = self.dist(st1, st2)

                # this also prevents spicing with pairs we already have
                if sid2 in matched[sid1] or sid1 in matched[sid2] or \
                        d > self.cutoff:
                    continue

                if self.clean_data and d < 250 and \
                        ((len(st1.name) > 2 and st1.name == st2.name) or
                         (len(st1.orig_nd_name) > 3 and
                          st1.orig_nd_name == st2.orig_nd_name)):
                    # dont negative-match station with an equivalent name
                    # and a distance < 100 - this is obviously an OSM
                    # mapping mistake, but we don't use it as ground truth
                    continue

                inserted = True

                matched[sid1].add(sid2)
                self.write_row(sid1, sid2, st1, st2, False, data, ind, iptr)

            if inserted:
                count += 1

    def prepare_features(self):
        if 'lev_simi' in self.features:
            self.lev_simi_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['lev_simi'] = self.lev_simi_idx

        if 'geodist' in self.features:
            self.geodist_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['geodist'] = self.geodist_idx

        if 'ped_simi_fw' in self.features:
            self.ped_simi_fw_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['ped_simi_fw'] = self.ped_simi_fw_idx

        if 'ped_simi_bw' in self.features:
            self.ped_simi_bw_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['ped_simi_bw'] = self.ped_simi_bw_idx

        if 'sed_simi_fw' in self.features:
            self.sed_simi_fw_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['sed_simi_fw'] = self.sed_simi_fw_idx

        if 'sed_simi_bw' in self.features:
            self.sed_simi_bw_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['sed_simi_bw'] = self.sed_simi_bw_idx

        if 'jaccard_simi' in self.features:
            self.jaccard_simi_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['jaccard_simi'] = self.jaccard_simi_idx

        if 'missing_ngram_count' in self.features:
            self.missing_ngram_count_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx[
                'missing_ngram_count'] = self.missing_ngram_count_idx

        if 'bts_simi' in self.features:
            self.bts_simi_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['bts_simi'] = self.bts_simi_idx

        if 'jaro_simi' in self.features:
            self.jaro_simi_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['jaro_simi'] = self.jaro_simi_idx

        if 'jaro_winkler_simi' in self.features:
            self.jaro_winkler_simi_idx = self.num_feats
            self.num_feats = self.num_feats + 1
            self.feature_idx['jaro_winkler_simi'] = self.jaro_winkler_simi_idx

        # number of features we use besides the ngram index
        self.num_feats = self.num_feats + 2 * self.num_pos_pairs
