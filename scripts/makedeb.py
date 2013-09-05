#!/usr/bin/python

import rpm
import os
import urlparse
import sys
import textwrap
import time
import re
import magic
import shutil
import subprocess
import shlex
import glob
import mappkgname

# BUGS:
#   Code is a mess
#   Hack to disable CFLAGS for ocaml-oclock
#   Hack to disable tests for ocaml-oclock
#   Hard coded install files only install ocaml dir
#   Should be building signed debs


# By default, RPM expects everything to be in $HOME/rpmbuild.  
# We want it to run in the current directory.
rpm.addMacro( '_topdir', os.getcwd() )


# Directories where rpmbuild expects to find inputs
# and writes outputs
top_dir = rpm.expandMacro( '%_topdir' )
rpm_dir = rpm.expandMacro( '%_rpmdir' )
spec_dir = rpm.expandMacro( '%_specdir' )
srpm_dir = rpm.expandMacro( '%_srcrpmdir' )
src_dir = rpm.expandMacro( '%_sourcedir' )
build_dir = rpm.expandMacro( '%_builddir' )

# Fedora puts executables run by other programs in /usr/libexec, but 
# Debian puts them in /usr/lib, which apparently follows the FHS:
# http://www.debian.org/doc/manuals/maint-guide/advanced.en.html#ftn.idp2018768
rpm.addMacro( '_libexecdir', "/usr/lib" )

# Override some macros interpolated into build rules, so
# paths are appropriate for debuild
# (Actually, using {}, not (), because these identifiers
# end up in helper scripts, not in the makefile
rpm.addMacro( "buildroot", "${DESTDIR}" )
rpm.addMacro( "_libdir", "/usr/lib" )


def specFromFile(spec):
    return rpm.ts().parseSpec(spec)


STANDARDS_VERSION = "3.9.3"


# Patches can be added to debian/patches, with a series file
# Files copied into this directory have to be added using dpkg-source --commit
# (possibly just initialize quilt in that directory and add them as we copy them)
# We can just use dpkg-source -b --auto-commit <dir>


def formatDescription(description):
    """need to format this - correct line length, initial one space indent,
    and blank lines must be replaced by dots"""

    paragraphs = "".join(description).split("\n\n")
    wrapped = [ "\n".join(textwrap.wrap( p, initial_indent=" ", 
                                            subsequent_indent=" ")) 
                for p in paragraphs ]
    return "\n .\n".join( wrapped )


def sourceDebFromSpec(spec):
    res = ""
    res += "Source: %s\n" % mappkgname.mapPackage(spec.sourceHeader['name'])[0]
    res += "Priority: %s\n" % "optional"
    res += "Maintainer: %s\n" % "Euan Harris <euan.harris@citrix.com>" #XXX
    res += "Section: %s\n" % mappkgname.mapSection(spec.sourceHeader['group'])
    res += "Standards-Version: %s\n" % STANDARDS_VERSION
    res += "Build-Depends:\n"
    build_depends = [ "debhelper (>= 8)", "dh-ocaml (>= 0.9)", "ocaml-nox" ]
    for pkg, version in zip(spec.sourceHeader['requires'], spec.sourceHeader['requireVersion']):
        deps = mappkgname.mapPackage(pkg)
        for dep in deps:
            if version:
                dep += " (>= %s)" % version
            build_depends.append(dep)
    res += ",\n".join( set([" %s" % d for d in build_depends]))
    res += "\n"
    return res


def binaryDebFromSpec(spec):
    res = ""
    res += "Package: %s\n" % mappkgname.mapPackageName(spec.header)
    res += "Architecture: %s\n"  % ("any" if spec.header['arch'] in [ "x86_64", "i686"] else "all")
    res += "Depends:\n"
    depends = ["${ocaml:Depends}", "${shlibs:Depends}", "${misc:Depends}"]
    for pkg, version in zip(spec.header['requires'], spec.header['requireVersion']):
        deps = mappkgname.mapPackage(pkg)
        for dep in deps:
            if version:
                dep += " (>= %s)" % version
            depends.append(dep)
    res += ",\n".join( [ " %s" % d for d in depends ] )
    res += "\n"
    res += "Provides: ${ocaml:Provides}\n"  # XXXX only for ocaml!
    res += "Recommends: ocaml-findlib\n" # XXXX 
    res += "Description: %s\n" % spec.header['summary']
    res += formatDescription( spec.header['description'] )
    res += "\n"
    return res


def debianControlFromSpec(spec):
    res = ""
    res += sourceDebFromSpec(spec)
    for pkg in spec.packages:
        res += "\n"
        res += binaryDebFromSpec(pkg)
    return res


def ocamlRulesPreamble():
    return """#!/usr/bin/make -f
#include /usr/share/cdbs/1/rules/debhelper.mk
#include /usr/share/cdbs/1/class/makefile.mk
#include /usr/share/cdbs/1/rules/ocaml.mk

export DH_VERBOSE=1
export DH_OPTIONS

export DESTDIR=$(CURDIR)/debian/tmp

%:
\tdh $@ --with ocaml

"""


def debianRulesFromSpec(spec, specpath, path):
    res = ""
    res += ocamlRulesPreamble()
    res += debianRulesConfigureFromSpec(spec)
    res += debianRulesBuildFromSpec(spec, path)
    res += debianRulesInstallFromSpec(spec, path)
    res += debianRulesDhInstallFromSpec(spec, specpath, path)
    res += debianRulesCleanFromSpec(spec, path)
    res += debianRulesTestFromSpec(spec, path)
    return res

    
# RPM doesn't have a configure target - everything happens in the build target
def debianRulesConfigureFromSpec(spec):
    # needed for OASIS packages with configure scripts
    # if debhelper sees a configure script it will assume it's from autoconf
    # and will run it with arguments that an OASIS configur script won't understand
    rule = ".PHONY: override_dh_auto_configure\n"
    rule += "override_dh_auto_configure:\n"
    return rule



def debianRulesBuildFromSpec(spec, path):
    # RPM's build script is just a script which is run at the appropriate time.
    # debian/rules is a Makefile.   Makefile recipes aren't shell scripts - each
    # line is run independently, so exports don't survive from line to line and
    # multi-line constructions such as if statements don't work.
    # Tried wrapping everything in a $(shell ...) function, but that didn't work.
    # Just write the script fragment into a helper script in the debian directory.
    # This almost certainly violates a Debian packaging guideline...
    # ...we could write them to temporary files as the makefile is evaluated...
    # this sub-script business unfortunately means that variables from the makefile
    # aren't passed through
    # Hurray, the .ONESHELL special target may save us

    if not spec.build:
        return ""
    rule = ".PHONY: override_dh_auto_build\n"
    rule += "override_dh_auto_build:\n"
    rule += "\tdebian/build.sh\n"
    rule += "\n"
    with open(os.path.join(path, "debian/build.sh"), "w") as f:
        helper = "#!/bin/sh\n"
        helper += "unset CFLAGS\n" #XXX HACK for ocaml-oclock
        helper += spec.build.replace("$RPM_BUILD_ROOT", "${DESTDIR}")
        f.write(helper)
    os.chmod(os.path.join(path, "debian/build.sh"), 0o755)
    return rule


def debianRulesInstallFromSpec(spec, path):
    rule = ".PHONY: override_dh_auto_install\n"
    rule += "override_dh_auto_install:\n"
    rule += "\tdebian/install.sh\n"
    rule += "\n"
    with open(os.path.join(path, "debian/install.sh"), "w") as f:
        f.write("#!/bin/sh\n" + spec.install.replace("$RPM_BUILD_ROOT", "${DESTDIR}"))
    os.chmod(os.path.join(path, "debian/install.sh"), 0o755)
    return rule

def debianRulesDhInstallFromSpec(spec, specpath, path):
    rule = ".PHONY: override_dh_install\n"
    rule += "override_dh_install:\n"
    rule += "\tdh_install\n"
    pkgname = mappkgname.mapPackageName(spec.sourceHeader)
    files = filesFromSpec(pkgname, specpath)
    if files.has_key( pkgname + "-%exclude" ):
        for pat in files[pkgname + "-%exclude"]:
            path = "\trm -f debian/%s/%s\n" % (pkgname, rpm.expandMacro(pat))
 	    rule += os.path.normpath(path)
    rule += "\n"
    return rule


def debianConfigFilesFromSpec(spec, specpath, path):
    pkgname = mappkgname.mapPackageName(spec.sourceHeader)
    files = filesFromSpec(pkgname, specpath)
    config_files = ""
    if files.has_key( pkgname + "-%config" ):
        for filename in files[pkgname + "-%config"]:
    	    config_files += "%s\n" % filename
    return config_files


def debianRulesTestFromSpec(spec, path):
    # XXX HACK for ocaml-oclock - don't try to run the tests when building
    rule = ".PHONY: override_dh_auto_test\n"
    rule += "override_dh_auto_test:\n"
    return rule


def debianRulesCleanFromSpec(spec, path):
    rule = ".PHONY: override_dh_auto_clean\n"
    rule += "override_dh_auto_clean:\n"
    rule += "\tdebian/clean.sh\n"
    rule += re.sub("^", "\t", spec.clean.strip(), flags=re.MULTILINE)
    rule += "\n"
    with open(os.path.join(path, "debian/clean.sh"), "w") as f:
        f.write("#!/bin/sh\n" + spec.clean.replace("$RPM_BUILD_ROOT", "${DESTDIR}"))
    os.chmod(os.path.join(path, "debian/clean.sh"), 0o755)
    return rule


def debianChangelogFromSpec(spec):
    hdr = spec.sourceHeader
    res = ""
    for (name, timestamp, text) in zip(hdr['changelogname'], hdr['changelogtime'], hdr['changelogtext']):

        # Most spec files have "First Last <first@foo.com> - version"
        # Some of ours have "First Last <first@foo.com>" only for the first entry - could
        # be a mistake.  For these, us the version from the spec. 
        m = re.match( "^(.+) - (\S+)$", name )
        if m:
            author = m.group(1)
            version = m.group(2)
        else:
            author = name
            version = "%s-%s" % (spec.sourceHeader['version'], spec.sourceHeader['release'])

        res += "%s (%s) UNRELEASED; urgency=low\n" % (mappkgname.mapPackage(hdr['name'])[0], version)
        res += "\n"
	text = re.sub( "^-", "*", text, flags=re.MULTILINE )
	text = re.sub( "^", "  ", text, flags=re.MULTILINE )
        res += "%s\n" % text
        res += "\n"
        res += " -- %s  %s\n" % (author, time.strftime("%a, %d %b %Y %H:%M:%S %z", time.gmtime(int(timestamp))))
        res += "\n"
    return res


def debianFilesFromPkg(basename, pkg, specpath):
    # should be able to build this from the files sections - can't find how
    # to get at them from the spec object
    res = ""
    #res += "ocaml/*        @OCamlStdlibDir@/\n"   # should be more specific
    files = filesFromSpec(basename, specpath)
    for l in files.get(pkg.header['name'], []):
        rpm.addMacro("_libdir", "usr/lib")
        rpm.addMacro("_bindir", "usr/bin")
        src = rpm.expandMacro(l).lstrip("/")  # deb just wants relative paths
        rpm.delMacro("_bindir")
        rpm.delMacro("_libdir")
        rpm.addMacro("_libdir", "/usr/lib")
        rpm.addMacro("_bindir", "/usr/bin")
        dst = rpm.expandMacro(l)
        # destination paths should be directories, not files.
        # if the file is foo and the path is /usr/bin/foo, the
        # package will end up install /usr/bin/foo/foo 
        if not dst.endswith("/"):
            dst = os.path.dirname(dst)
        rpm.delMacro("_bindir")
        rpm.delMacro("_libdir")
        res += "%s %s\n" % (src, dst)
    return res


def debianFilelistsFromSpec(spec, path, specpath):
    for pkg in spec.packages:
        name = "%s.install.in" % mappkgname.mapPackageName(pkg.header)
        with open( os.path.join(path, "debian/%s") % name, "w" ) as f:
            f.write( debianFilesFromPkg(spec.sourceHeader['name'], pkg, specpath) )

def debianPatchesFromSpec(spec, path):
    patches = [(seq, name) for (name, seq, typ) in spec.sources 
               if typ == 2]
    patches = [name for (seq,name) in sorted(patches)]
    if patches:
        os.mkdir(os.path.join(path, "debian/patches"))
    for patch in patches:
        shutil.copy2(os.path.join(src_dir, patch), os.path.join(path, "debian/patches")) 
        with open( os.path.join(path, "debian/patches/series"), "a" ) as f:
            f.write("%s\n" % patch)



def debianDirFromSpec(spec, path, specpath, isnative):
    os.makedirs( os.path.join(path, "debian/source") )

    with open( os.path.join(path, "debian/control"), "w" ) as control:
        control.write(debianControlFromSpec(spec))

    with open( os.path.join(path, "debian/rules"), "w" ) as rules:
        rules.write(debianRulesFromSpec(spec, specpath, path))
    os.chmod( os.path.join(path, "debian/rules"), 0o755 )

    with open( os.path.join(path, "debian/compat"), "w" ) as compat:
        compat.write("8\n")

    with open( os.path.join(path, "debian/source/format"), "w" ) as format:
        if isnative:
            format.write("3.0 (native)\n")
        else:
            format.write("3.0 (quilt)\n")

    with open( os.path.join(path, "debian/copyright"), "w" ) as copyright:
        copyright.write("FIXME")

    with open( os.path.join(path, "debian/changelog"), "w" ) as changelog:
        changelog.write(debianChangelogFromSpec(spec))

    debianFilelistsFromSpec(spec, path, specpath)
    debianPatchesFromSpec(spec, path)

    configs = debianConfigFilesFromSpec(spec, specpath, path)
    if configs:
        with open( os.path.join(path, "debian/conffiles"), "w" ) as conffiles:
            conffiles.write(configs)

def principalSourceFile(spec):
    return os.path.basename([name for (name, seq, type) in spec.sources 
                             if seq == 0 and type == 1][0])

def prepareBuildDir(spec, build_subdir):
    # To prepare the build dir, RPM cds into $TOPDIR/BUILD
    # and expands all paths in the prep script with $TOPDIR.
    # It unpacks the tarball and then cds into the directory it
    # creates before applying patches.
    # $TOPDIR should be an absolute path to the top RPM build
    # directory, not a relative path, so that references to SOURCES
    # expand to reachable paths inside the source tree (getting the 
    # tarball from ../SOURCES works in the outer BUILD dir, but getting
    # patches from ../SOURCES doesn't work when we have cd'ed into the
    # source tree.

    unpack_dir = os.path.join(build_dir, build_subdir)
    subprocess.call(spec.prep.replace("$RPM_BUILD_ROOT", unpack_dir), shell=True)
    # could also just do: RPMBUILD_PREP = 1<<0; spec._doBuild()

    
def renameSource(origfilename, pkgname, pkgversion):
    # Debian source package name should probably match the tarball name
    origfilename = principalSourceFile(spec)
    if origfilename.endswith(".tbz"):
        filename = origfilename[:-len(".tbz")] + ".tar.bz2"
    else:
        filename = origfilename
    m = re.match( "^(.+)(\.tar\.(gz|bz2|lzma|xz))", filename )
    if not m:
        print "error: could not parse filename %s" % filename
    basename, ext = m.groups()[:2]
    baseFileName = "%s_%s.orig%s" % (mappkgname.mapPackage(pkgname)[0], pkgversion, ext)
    shutil.copy(os.path.join(src_dir, origfilename), os.path.join(build_dir, baseFileName))


def filesFromSpec(basename, specpath):
    """The RPM library doesn't seem to give us access to the files section,
    so we need to go and get it ourselves.   This parsing algorithm is
    based on build/parseFiles.c in RPM.   The list of section titles
    comes from build/parseSpec.c.   We should get this by using ctypes
    to load the rpm library."""
    """XXX shouldn't be parsing this by hand.   will need to handle conditionals
    within and surrounding files and packages sections."""

    #XXX Why do we need the .install.in files?

    otherparts = [ 
        "%package", 
        "%prep", 
        "%build", 
        "%install", 
        "%check", 
        "%clean", 
        "%preun", 
        "%postun", 
        "%pretrans", 
        "%posttrans", 
        "%pre", 
        "%post", 
        "%changelog", 
        "%description", 
        "%triggerpostun", 
        "%triggerprein", 
        "%triggerun", 
        "%triggerin", 
        "%trigger", 
        "%verifyscript", 
        "%sepolicy", 
    ]

    files = {}
    with open(specpath) as spec:
        inFiles = False
        section = ""
        for line in spec:
            tokens = line.strip().split(" ")
            if tokens and tokens[0].lower() == "%files":
                section = basename
                inFiles = True
                if len(tokens) > 1:
                    section = basename + "-" + tokens[1]
                continue
                
            if tokens and tokens[0] in otherparts:
                inFiles = False

            if inFiles:
                if tokens[0].lower().startswith("%defattr"):
                    continue
                if tokens[0].lower().startswith("%attr"):
                    continue
                if tokens[0].lower() == "%doc":
                    docsection = section + "-doc"
                    files[docsection] = files.get(docsection, []) + tokens[1:]
                    continue
                if tokens[0].lower() == "%if" or tokens[0].lower() == "%endif":
                    # XXX evaluate the if condition and do the right thing here
                    continue
                if tokens[0].lower() == "%exclude":
                    excludesection = section + "-%exclude"
                    files[excludesection] = files.get(excludesection, []) + tokens[1:]
                    continue
                if tokens[0].lower().startswith("%config"):
                    # dh_install automatically considers files in /etc to be config files 
                    # so we don't have to do anythin special for them
                    # The spec file documentation says that a %config directive can
                    # only apply to a single file.
                    configsection = section + "-%config"
                    if tokens[1].startswith("/etc"):
                        files[section] = files.get(section, []) + tokens[1:]
                    else:
                        files[configsection] = files.get(configsection, []) + tokens[1:]
                    continue
                if tokens[0].startswith("%config"):
                    # XXX do the right thing here - should add to debian/configfiles
                    continue
                if line.strip():
                    files[section] = files.get(section, []) + [line.strip()]
        return files
            


# Instead of writing to all these files, we could just accumulate
# everything in a dictionary of path -> (content, perms) and then
# write the files at the end.

if __name__ == '__main__':
    shutil.rmtree(build_dir)   #XXX
    os.mkdir(build_dir)
    spec = specFromFile(sys.argv[1])
    clean = True
    if "-noclean" in sys.argv:
        clean = False

    # subdirectory of builddir in which the tarball is unpacked;  
    # set by RPM after processing the spec file
    # if the source file isn't a tarball this won't be set!
    build_subdir = rpm.expandMacro("%buildsubdir")  
    prepareBuildDir(spec, build_subdir)

    if os.path.isdir( os.path.join(build_dir, build_subdir, "debian") ):
        shutil.rmtree(os.path.join(build_dir, build_subdir, "debian"))

    # a package with no original tarball is built as a 'native debian package'
    native = True
    tarball = principalSourceFile(spec)
    m = re.match( "^(.+)((\.tar\.(gz|bz2|lzma|xz)|\.tbz)$)", tarball )
    if m:
        native = False
         

        # copy over the source, run the prep rule to unpack it, then rename it as deb expects
        # this should be based on the rewritten (or not) source name in the debian package - build the debian dir first and then rename the tarball as needed
        renameSource(tarball, spec.sourceHeader['name'], spec.sourceHeader['version']) 

    debianDirFromSpec(spec, os.path.join(build_dir, build_subdir), sys.argv[1], native)

    # pdebuild gives us source debs as well as binaries
    res = subprocess.call( "cd %s\ndpkg-source -b --auto-commit %s" % (build_dir, build_subdir), shell=True )
    #pbuild can build a dsc - pbuilder --build <dsc> --configfile pbuilder/pbuildrerc-amd64 --resultdir ...
    #res = subprocess.call( "cd %s\npdebuild --configfile %s --buildresult %s" % (os.path.join(build_dir, build_subdir), os.path.join(top_dir, "pbuilder/pbuilderrc-amd64"), rpm_dir), shell=True )
    assert res == 0
    if clean:
        shutil.rmtree(os.path.join(build_dir, build_subdir))
    for i in glob.glob(os.path.join(build_dir, "*")):
        if build_subdir in i:
            continue
        shutil.copy2(i, srpm_dir) #XXX
        if clean:
            os.unlink(i)

    # at this point we have a debian source package (at least 3 files) in SRPMS
    # to build it:
    #    (first time: pbuilder --create)
    #    dpkg-source -x SRPMS/ocaml-react-0.9.4.dsc
    #    cd ocaml-react-0.9.4
    #    pdebuild --config pbuilder/pbuilder.cfg --buildresult DEBS  (can we set up groups to avoid the sudo password prompt?)
    #
    # to build for arm:
    #    pbuilder --create --distribution raring --architecture armhf --debootstrap qemu-debootstrap --mirror http://ports.ubuntu.com --basetgz /var/cache/pbuilder/qemu-raring-armhf-base.tar.gz
    #    pdebuild -- --distribution raring --architecture armhf --debootstrap qemu-debootstrap --mirror http://ports.ubuntu.com --basetgz /var/cache/pbuilder/qemu-raring-armhf-base.tar.gz


