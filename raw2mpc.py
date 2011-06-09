# Copyright (C) 2010-2011 Richard Lincoln
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

"""Raw data file to MATPOWER case converter."""

import sys
import os
import logging
import optparse
import csv

from numpy import zeros, r_

from scipy.io import savemat


DEFAULT_VERSION = 30
SUPPORTED_VERSIONS = [29, 30 , 31, 32]

logger = logging.getLogger(__name__)


def raw2mpc(pf_rawfile, opf_rawfile=None, matfile=None, ver=None, delim=None):
    """Raw data file to MATPOWER case converter.

    Converts Raw format data in the given file or file-like object into a
    dictionary containing MATPOWER case data matrices for 'bus', 'gen',
    'branch'. If an Optimal Power Flow Raw data file or file-like object
    is specified the case will include a 'gencost' key. If C{matfile} is
    specified the data is saved as a struct in MAT-file format.
    """
    mpc = {}
    status = 0

    ## Power Flow Raw Data ##
    pf_mpc = None
    if isinstance(pf_rawfile, basestring):
        fname = os.path.basename(pf_rawfile)
        logger.info("Loading Power Flow Raw file [%s]." % fname)

        fd = None
        try:
            fd = open(pf_rawfile, "rb")
        except IOError, e:
            logger.error("Error opening %s.\n%s" % (fname, e.message))
            status = 1
        finally:
            if fd is not None:
                pf_mpc = pf2mpc(fd, ver, delim)
                fd.close()
    else:
        pf_mpc = pf2mpc(pf_rawfile, ver, delim)

    if pf_mpc is not None:
        mpc.update(pf_mpc)


    ## Optimal Power Flow Raw Data ##
    opf_mpc = None
    if isinstance(opf_rawfile, basestring):
        fname = os.path.basename(opf_rawfile)
        logger.info("Loading OPF Raw file [%s]." % fname)

        fd = None
        try:
            fd = open(opf_rawfile, "rb")
        except IOError, e:
            logger.error("Error opening %s.\n%s" % (fname, e.message))
            status = 2
        finally:
            if fd is not None:
                opf_mpc = opf2mpc(fd, ver, delim)
                fd.close()
    elif opf_rawfile is not None:
        opf_mpc = opf2mpc(opf_rawfile, ver, delim)

    if opf_mpc is not None:
        mpc.update(opf_mpc)


    ## Save MAT-file ##
    if matfile is not None and len(mpc) > 0:
        try:
            savemat(matfile, {"mpc": mpc}, oned_as="col")
        except IOError, e:
            logger.error("Error saving MAT-file %s.\n%s" % (matfile, e.message))
            status = 3

    return mpc, status


def pf2mpc(fd, version, delimiter):
    sep = _delimiter(fd) if delimiter is None else delimiter
    rev = _version(fd, sep) if version is None else version

    fd.seek(0)
    reader = csv.reader(fd, delimiter=sep, skipinitialspace=True)

    baseMVA = _case_identification(reader)

    busdata, busmap = _bus_data(reader, rev)
    _load_data(reader, busdata, busmap, rev)

    if rev in [31, 32]:
        _fixed_shunt_data(reader)

    gendata = _generator_data(reader, busmap)

    branchdata = _nontransformer_branch_data(reader, busdata, busmap)
    _transformer_data(reader, branchdata, version, busmap)

    _area_interchange_data(reader)
    _two_terminal_dc_line_data(reader)
    _vsc_dc_lines()

    if rev in [29, 30]:
        _switched_shunt_data(reader, busdata)

    _transformer_impedance_correction_tables()
    _multi_terminal_dc_transmission_line_data()
    _multi_section_line_grouping_data()
    _zone_data()
    _interarea_transfer_data()
    _owner_data()
    _facts_device_data()

    if rev in [31, 32]:
        _switched_shunt_data()

    if rev == 32:
        _q_record()

    mpc = {
        "baseMVA": baseMVA,
        "bus": busdata,
        "gen": gendata,
        "branch": branchdata
    }

    return mpc


def _case_identification(reader):
    """Reads the first three lines of the file and returns the system base MVA.
    """
    h0 = reader.next()
    _ = reader.next()
    _ = reader.next()

    assert (h0[0] == "0") or (h0[0] == "1")

    # v29-30: IC, SBASE, REV / COMMENT
    # v31-32: IC, SBASE, REV, XFRRAT, NXFRAT, BASFRQ / COMMENT
    baseMVA = float(h0[1])

    return baseMVA


def _bus_data(reader, version):
    # v29-30: I, 'NAME', BASKV, IDE, GL, BL, AREA, ZONE, VM, VA, OWNER
    # v31-32: I, 'NAME', BASKV, IDE, AREA, ZONE, OWNER, VM, VA
    # bus_i type Pd Qd Gs Bs area Vm Va baseKV zone Vmax Vmin

    buscol = 13
    buses = zeros((0, buscol))
    busmap = {}
    c = 0

    busdata = reader.next()
    # 0 / END OF BUS DATA, BEGIN LOAD DATA
    while busdata[0].split("/")[0].strip() != "0":
        bus = zeros((1, buscol))

        # Map bus number and name to bus data index.
        i = busdata[0]
        name = busdata[1].strip("'")
        busmap[i] = c
        busmap[name] = c

        bus[0, 0] = int(i)
        bus[0, 1] = float(busdata[3]) # type
        bus[0, 2] = 0.0 # Pd (see _parse_loads)
        bus[0, 3] = 0.0 # Qd (see _parse_loads)
        if version in [29, 30]:
            bus[0, 4] = float(busdata[4])  # Gs
            bus[0, 5] = float(busdata[5])  # Bs
            bus[0, 6] = int(busdata[6])    # area
            bus[0, 7] = float(busdata[8])  # Vm
            bus[0, 8] = float(busdata[9])  # Va
            bus[0, 10] = float(busdata[7]) # zone
        elif version in [31, 32]:
            bus[0, 6] = int(busdata[4])    # area
            bus[0, 7] = float(busdata[7])  # Vm
            bus[0, 8] = float(busdata[8])  # Va
            bus[0, 10] = float(busdata[5]) # zone
        bus[0, 9] = float(busdata[2]) # baseKV
        bus[0, 11] = 1.1 # Vmax
        bus[0, 12] = 0.9 # Vmin

        buses = r_[buses, bus]
        busdata = reader.next()
        c += 1

    logger.info("%d bus data records." % c)

    return buses, busmap


def _load_data(reader, bus, busmap, version):
    # v29-31: I, ID, STATUS, AREA, ZONE, PL, QL, IP, IQ, YP, YQ, OWNER
    # v32:    I, ID, STATUS, AREA, ZONE, PL, QL, IP, IQ, YP, YQ, OWNER, SCALE
    c = 0
    loaddata = reader.next()
    # 0 / END OF LOAD DATA, BEGIN GENERATOR DATA
    while loaddata[0].split("/")[0].strip() != "0":
        status = bool(loaddata[2])
        i = loaddata[0]
        idx = _busidx(i, busmap)

        if (status == True) and (idx != None):

            # bus_i type Pd Qd Gs Bs area Vm Va baseKV zone Vmax Vmin
            bus[idx, 2] = float(loaddata[5]) # Pd PL
            bus[idx, 3] = float(loaddata[6]) # Qd QL

            Ip = float(loaddata[7])
            Iq = float(loaddata[8])
            if Ip or Iq:
                logger.warning("Constant current load of %.2fMW (%.2fMVAr) at "
                               "bus %s (%d) ignored." % (Ip, Iq, i, idx))
            Yp = float(loaddata[9])
            Yq = float(loaddata[10])
            if Yp or Yq:
                logger.warning("Constant admittance load of %.2fMW (%.2fMVAr) "
                               "at bus %s (%d) ignored." % (Yp, Yq, i, idx))

            if version == 32:
                scale = float(loaddata[12])
                if (scale != 0.0) or (scale != 1.0):
                    logger.warning("Load at bus %s (%d) not scaled by %.2f." %
                                   (i, idx, scale))

        loaddata = reader.next()
        c += 1

    logger.info("%d load data records." % c)

    return bus


def _generator_data(reader, busmap):
    # v29-30: I,ID,PG,QG,QT,QB,VS,IREG,MBASE,ZR,ZX,RT,XT,GTAP,STAT,RMPCT,PT,PB,
    #         O1,F1,....O4,F4
    # v31-32: I,ID,PG,QG,QT,QB,VS,IREG,MBASE,ZR,ZX,RT,XT,GTAP,STAT,RMPCT,PT,PB,
    #         O1,F1,...,O4,F4,WMOD,WPF
    # bus, Pg, Qg, Qmax, Qmin, Vg, mBase, status, Pmax, Pmin, Pc1, Pc2,
    # Qc1min, Qc1max, Qc2min, Qc2max, ramp_agc, ramp_10, ramp_30, ramp_q, apf
    gencol = 21
    generators = zeros((0, gencol))

    gendata = reader.next()
    # 0 / END OF GENERATOR DATA, BEGIN NON-TRANSFORMER BRANCH DATA
    while gendata[0].split("/")[0].strip() != "0":
        i = gendata[0]
        idx = _busidx(i, busmap)

        if idx != None:
            gen = zeros((1, gencol))

            gen[0, 1] = float(gendata[2]) # Pg
            gen[0, 2] = float(gendata[3]) # Qg
            gen[0, 3] = float(gendata[4]) # Qmax
            gen[0, 4] = float(gendata[5]) # Qmin
            gen[0, 5] = float(gendata[6]) # Vg
            gen[0, 6] = float(gendata[8]) # mBase
            gen[0, 7] = float(gendata[14]) # status
            gen[0, 8] = float(gendata[16]) # Pmax
            gen[0, 9] = float(gendata[17]) # Pmin

            generators = r_[generators, gen]

        gendata = reader.next()

    logger.info("%d generator records." % generators.shape[0])

    return generators


def _nontransformer_branch_data(reader, bus, busmap):
    # v29-30: I,J,CKT,R,X,B,RATEA,RATEB,RATEC,GI,BI,GJ,BJ,ST,
    #         LEN,O1,F1,...,O4,F4
    # v31-32: I,J,CKT,R,X,B,RATEA,RATEB,RATEC,GI,BI,GJ,BJ,ST,
    #         MET,LEN,O1,F1,...,O4,F4
    # fbus,tbus,r,x,b,rateA,rateB,rateC,ratio,angle,status,angmin,angmax
    brchcol = 13
    branches = zeros((0, brchcol))

    brchdata = reader.next()
    while brchdata[0].split("/")[0].strip() != "0":
        fbus = _busidx(brchdata[0], busmap)
        tbus = _busidx(brchdata[1], busmap)

        if (fbus != None) and (tbus != None):
            brch = zeros((1, brchcol))

            brch[0, 0] = bus[fbus, 0] # fbus
            brch[0, 1] = bus[tbus, 1] # tbus
            brch[0, 2] = float(brchdata[3]) # r
            brch[0, 3] = float(brchdata[4]) # x
            brch[0, 4] = float(brchdata[5]) # b
            brch[0, 5] = float(brchdata[6]) # rateA
            brch[0, 6] = float(brchdata[7]) # rateB
            brch[0, 7] = float(brchdata[8]) # rateC
            brch[0, 10] = float(brchdata[13]) # status
            brch[0, 11] = -360.0 # angmin
            brch[0, 12] =  360.0 # angmax

            branches = r_[branches, brch]

        brchdata = reader.next()

    logger.info("%d non-transformer branch records." % branches.shape[0])

    return branches


def _transformer_data(reader, branch, version, busmap):
    pass


def _switched_shunt_data(reader, bus, version, busmap):
    # v29-30: I,MODSW,VSWHI,VSWLO,SWREM,RMPCT,'RMIDNT',BINIT,
    #         N1,B1,N2,B2,...N8,B8
    # v31:    I,MODSW,VSWHI,VSWLO,SWREM,RMPCT,'RMIDNT',BINIT,
    #         N1,B1,N2,B2,...N8,B8
    # v32:    I,MODSW,ADJM,STAT,VSWHI,VSWLO,SWREM,RMPCT,'RMIDNT',BINIT,
    #         N1,B1,N2,B2,...N8,B8
    c = 0
    shuntdata = reader.next()

    while shuntdata[0].split("/")[0].strip() != "0":
        if version == 32:
            status = bool(shuntdata[2])
        else:
            status = True

        idx = _busidx(shuntdata[0], busmap)

        if (status == True) and (idx != None):
            # bus_i type Pd Qd Gs Bs area Vm Va baseKV zone Vmax Vmin
            if version == 32:
                bs = float(shuntdata[9])
            else:
                bs = float(shuntdata[7])
            bus[idx, 5] += bs

        shuntdata = reader.next()
        c += 1

    logger.info("%d switched shunt data records." % c)

    return bus


def _fixed_shunt_data(reader):
    pass
def _area_interchange_data(reader):
    pass
def _two_terminal_dc_line_data(reader):
    pass
def _vsc_dc_lines(reader):
    pass
def _transformer_impedance_correction_tables(reader):
    pass
def _multi_terminal_dc_transmission_line_data(reader):
    pass
def _multi_section_line_grouping_data(reader):
    pass
def _zone_data(reader):
    pass
def _interarea_transfer_data(reader):
    pass
def _owner_data(reader):
    pass
def _facts_device_data(reader):
    pass
def _q_record(reader):
    pass

## OPF Raw Data File ##

def opf2mpc(fd, version, delim):
    return {}

def _data_modification_code():
    pass
def _bus_voltage_attribute_data():
    pass
def _adjustable_bus_shunt_data():
    pass
def _bus_load_data():
    pass
def _adjustable_bus_load_table_data():
    pass
def _generator_dispatch_data():
    pass
def _active_power_dispatch_table_data():
    pass
def _generation_reserve_data():
    pass
def _generation_reactive_capability_data():
    pass
def _adjustable_branch_reactance_data():
    pass
def _piecewise_linear_cost_tables():
    pass
def _piecewise_quadratic_cost_tables():
    pass
def _polynomial_and_exponential_cost_tables():
    pass
def _period_reserve_constraint_data():
    pass
def _branch_flow_constraint_data():
    pass
def _interface_flow_constraint_data():
    pass
def _linear_constraint_dependency_data():
    pass

## Utilities ##

def _busidx(i, busmap):
    i = i.strip("'")

    if i in busmap:
        return busmap[i]
    else:
        logger.error("Bus [%s] not found" % i)

    return None


def _delimiter(fd):
    """Uses the first line to determine if data items are separated by a comma
    or one or more blank spaces.

    @rtype: A one-character string.
    @return: Either ',' or ' '.
    """
    fd.seek(0)
    # v29-30: IC, SBASE, REV / COMMENT
    # v31-32: IC, SBASE, REV, XFRRAT, NXFRAT, BASFRQ / COMMENT
    header0 = fd.next().split("/")[0]

    if "," in header0:
        logger.info("Found comma delimited data items.")
        delimiter = ","
    else:
        logger.info("Found data items separated by whitespace.")
        delimiter = " "

    return delimiter


def _version(fd, delimiter):
    """Uses the first line to determine the data format version or returns the
    default version.

    @rtype: int
    @return: Raw data format version.
    """
    fd.seek(0)
#    header0 = file.next().split("/")[0]
    reader = csv.reader(fd, delimiter=delimiter, skipinitialspace=True)

    h0 = reader.next()
    if len(h0) < 3:
        version = DEFAULT_VERSION
        logger.info("No version info found, assuming version %d." % version)
    else:
        # v29-30: IC, SBASE, REV / COMMENT
        # v31-32: IC, SBASE, REV, XFRRAT, NXFRAT, BASFRQ / COMMENT
        if "/" in h0[2]:
            version = int( h0[2].split("/")[0].strip() )
        else:
            version = int(h0[2])
        logger.info("Version %d data found." % version)
        if version not in SUPPORTED_VERSIONS:
            logger.warning("Version %d data not currently supported. "
                "Supported versions are: %s "
                "Attempting to parse file as version %d data." %
                (version, SUPPORTED_VERSIONS, DEFAULT_VERSION))
            version = DEFAULT_VERSION

    return version


def main(argv=sys.argv[1:]):
    parser = optparse.OptionParser(
        usage="usage: raw2mpc [options] input_file")

    parser.add_option("-o", "--output", dest="output", metavar="FILE",
        help="Write the case to FILE.")

    parser.add_option("-v", "--verbose", action="store_true", dest="verbose",
        default=False, help="Print more information.")

    parser.add_option("-d", "--debug", action="store_true", dest="debug",
        default=False, help="Print debug information.")

    parser.add_option("-r", "--revision",
        metavar="REV", dest="revision",
        help="Indicates the Raw file format version. The "
        "versions which are currently supported are: %s  If no version "
        "is specified then an attempt to determine the value from the "
        "file header is made. If unsuccessful the default version [%s] is "
        "used." % (SUPPORTED_VERSIONS, DEFAULT_VERSION))

    parser.add_option("-s", "--separator",
        metavar="SEP", dest="delimiter",
        help="Indicates how data items are separated in the case file. The "
        "types which are supported are: 'comma' and 'space'  If no separator "
        "is specified then it is determined from the file header.")

    options, args = parser.parse_args(argv)

    ## Logging Level ##
    level = logging.INFO if options.verbose else logging.WARNING
    if options.debug:
        level = logging.DEBUG
    logging.basicConfig(level=level)

    ## Raw File Format Revision ##
    if options.revision:
        revision = int(options.revision)
    else:
        revision = None

    ## Raw File Delimiter ##
    if options.delimiter:
        if options.delimiter == "comma":
            delimiter = ","
        elif options.delimiter == "space":
            delimiter = " "
        else:
            logger.warning("Invalid delimiter [%s]." % options.delimiter)
            delimiter = None
    else:
        delimiter = None

    ## Input PF and OPF Raw Data Files ##
    if len(args) == 1:
        pf_rawfile = args[0]
        opf_rawfile = None
    elif len(args) == 2:
        pf_rawfile = args[0]
        opf_rawfile = args[1]
    else:
        parser.print_help()
        return 2


    ## Output Matlab/GNU Octave MAT-file ##
    if options.output:
        matfile = options.output
    else:
        root, _ = os.path.splitext(pf_rawfile)
        matfile = root + ".mat"


    _, status = raw2mpc(pf_rawfile, opf_rawfile, matfile, revision, delimiter)

    return status


if __name__ == "__main__":
    sys.exit(main())
