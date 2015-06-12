#!/usr/bin/python -tt
# -*- coding: utf-8 -*-

# (c) 2013, Patrick Callahan <pmc@patrickcallahan.com>
# (c) 2015, Matt Davis <mdavis@ansible.com>
# based on
#     openbsd_pkg
#         (c) 2013
#         Patrik Lundin <patrik.lundin.swe@gmail.com>
#
#     yum
#         (c) 2012, Red Hat, Inc
#         Written by Seth Vidal <skvidal at fedoraproject.org>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import re, sys
from distutils.version import LooseVersion

DOCUMENTATION = '''
---
module: zypper
author: Patrick Callahan
version_added: "1.2"
short_description: Manage packages on SUSE and openSUSE
description:
    - Manage packages on SUSE and openSUSE using the zypper and rpm tools.
options:
    name:
        description:
        - package name or package specifier wth version C(name) or C(name-1.0).
        required: true
        aliases: [ 'pkg' ]
    state:
        description:
          - C(present) will make sure the package is installed.
            C(latest)  will make sure the latest version of the package is installed.
            C(absent)  will make sure the specified package is not installed.
        required: false
        choices: [ present, latest, absent ]
        default: "present"
    disable_gpg_check:
        description:
          - Whether to disable to GPG signature checking of the package
            signature being installed. Has an effect only if state is
            I(present) or I(latest).
        required: false
        default: "no"
        choices: [ "yes", "no" ]
        aliases: []
    disable_recommends:
        version_added: "1.8"
        description:
          - Corresponds to the C(--no-recommends) option for I(zypper). Default behavior (C(yes)) modifies zypper's default behavior; C(no) does install recommended packages. 
        required: false
        default: "yes"
        choices: [ "yes", "no" ]


notes: []
# informational: requirements for nodes
requirements: [ zypper, rpm ]
author: Patrick Callahan
'''

EXAMPLES = '''
# Install "nmap"
- zypper: name=nmap state=present

# Install apache2 with recommended packages
- zypper: name=apache2 state=present disable_recommends=no

# Remove the "nmap" package
- zypper: name=nmap state=absent

# Install specific versions of nmap and wireshark (using zypper's version syntax)
- zypper: name=nmap=4.75-1.30,wireshark=1.10.13
'''

debug_messages = []
debug_to_stderr = False

class PkgSpec(object):
    # eg: my-package-name
    # my-package-name>=1.2.3
    # my-package-name=1.2.3.4-1.0
    _spec_parse_re = re.compile('^(?P<pkg>[^=><]+)(?P<op>>=|<=|=|>|<)?(?P<ver>[^-]+)?-?(?P<rel>.+)?$')

    def __init__(self, pkgspec):
        self.rawspec = pkgspec

        match = self._spec_parse_re.match(pkgspec)

        if not match:
            raise Exception('invalid package spec: %s' % pkgspec)

        self.pkg = match.group('pkg')
        self.op = match.group('op')

        if self.op and self.op != '=':
            raise Exception('unsupported package spec operator %s' % self.op)

        self.version = match.group('ver')
        self.release = match.group('rel')

    # NB: roughly equivalent to rpm version comparision- could take an rpm-python dependency or re-implement to make it exact...
    def satisfies_spec(self, packagename, version, release):
        if self.pkg != packagename:
            return False

        # only include release if the spec included it- otherwise pkg=2.0 != pkg-2.0-0
        if self.version and self.release:
            spec_ver = LooseVersion('%s-%s' % (self.version, self.release))
            candidate_ver = LooseVersion('%s-%s' % (version, release))
        elif self.version:
            spec_ver = LooseVersion(self.version)
            candidate_ver = LooseVersion(version)
        else: # no version specified, just a name match- done!
            return True

        # TODO: implement spec operators beyond =
        if spec_ver == candidate_ver:
            return True

        return False


# Function used for getting zypper version
def zypper_version(module):
    """Return (rc, message) tuple"""
    cmd = ['/usr/bin/zypper', '-V']
    rc, stdout, stderr = module.run_command(cmd, check_rc=False)
    if rc == 0:
        return rc, stdout
    else:
        return rc, stderr

# get currently installed version/release of requested packages
def get_installed_versions(m, pkgspecs):
    cmd = ['/bin/rpm', '-q', '--qf', 'package %{NAME} is installed version %{VERSION} release %{RELEASE}\n']
    cmd.extend([p.pkg for p in pkgspecs])
    (rc, stdout, stderr) = m.run_command(cmd)

    write_debug('rpm command: %s' % cmd)

    current_versions = {}
    rpmoutput_re = re.compile('^package (?P<pkg>\S+) is installed version (?P<ver>\S+) release (?P<rel>\S+)$')
    for stdoutline, pkgspec in zip(stdout.splitlines(), pkgspecs):

        m = rpmoutput_re.match(stdoutline)

        if m == None:
            current_versions[pkgspec.pkg] = None
        else:
            rpmpackage = m.group('pkg')
            rpmversion = m.group('ver')
            rpmrelease = m.group('rel')

            # TODO: this case should probably be an error- we expect the rpm output to come in the same order...
            if pkgspec.pkg != rpmpackage:
                #current_versions[pkgspec.pkg] = None
                raise Exception('package mismatch (expected %s, got %s)' % (pkgspec.pkg, rpmpackage))

            current_versions[pkgspec.pkg] = (rpmversion,rpmrelease)

    return current_versions


# Function used to make sure a package is present.
def package_present(m, pkgspecs, installed_state, disable_gpg_check, disable_recommends, old_zypper):
    packages_to_install = []
    for pkgspec in pkgspecs:
        pkgstate = installed_state[pkgspec.pkg]
        installedpkg = pkgspec.pkg if pkgstate else None
        installedver = pkgstate[0] if pkgstate else None
        installedrel = pkgstate[1] if pkgstate else None

        if not pkgspec.satisfies_spec(installedpkg, installedver, installedrel):
            # zypper install is the only thing that natively understands this format
            packages_to_install.append(pkgspec.rawspec)
    if len(packages_to_install) > 0:
        cmd = ['/usr/bin/zypper', '--non-interactive']
        # add global options before zypper command
        if disable_gpg_check:
            cmd.append('--no-gpg-checks')
        # TODO: add allow_downgrades (defaulted to yes)
        cmd.extend(['install', '--auto-agree-with-licenses', '-t', package_type, '--oldpackage'])
        # add install parameter
        if disable_recommends and not old_zypper:
            cmd.append('--no-recommends')
        cmd.extend(packages_to_install)

        write_debug('zypper install command: %s' % cmd)

        if not m.check_mode:
            rc, stdout, stderr = m.run_command(cmd, check_rc=False)
        else:
            rc = 0
            stdout = ''
            stderr = ''
            changed = True

        # TODO: this check is broken- zypper returns non-zero in lots of success cases
        if rc == 0:
            changed=True
        else:
            changed=False
    else:
        rc = 0
        stdout = ''
        stderr = ''
        changed=False

    return (rc, stdout, stderr, changed)

# Function used to make sure a package is the latest available version.
def package_latest(m, pkgspecs, installed_state, disable_gpg_check, disable_recommends, old_zypper):

    # first of all, make sure all the packages are installed
    (rc, stdout, stderr, changed) = package_present(m, pkgspecs, installed_state, disable_gpg_check, disable_recommends, old_zypper)

    # if we've already made a change, we don't have to check whether a version changed
    if not changed:
        pre_upgrade_versions = get_installed_versions(m, pkgspecs)

    cmd = ['/usr/bin/zypper', '--non-interactive']

    if disable_gpg_check:
        cmd.append('--no-gpg-checks')

    if old_zypper:
        cmd.extend(['install', '--auto-agree-with-licenses'])
    else:
        cmd.extend(['update', '--auto-agree-with-licenses'])

    if m.check_mode:
        cmd.append('--dry-run')

    # only pass the package names for 'latest'...
    cmd.extend([pkgspec.pkg for pkgspec in pkgspecs])

    write_debug("zypper latest command: %s" % cmd)

    rc, stdout, stderr = m.run_command(cmd, check_rc=False)

    if m.check_mode and not changed:
        # TODO: come up with a better way?
        changed = stdout.find('is going to be upgraded') >= 0
    else:
        # if we've already made a change, we don't have to check whether a version changed
        if not changed:
            post_upgrade_versions = get_installed_versions(m, pkgspecs)

            if pre_upgrade_versions != post_upgrade_versions:
                changed = True

    return (rc, stdout, stderr, changed)

# Function used to make sure a package is not installed.
def package_absent(m, pkgspecs, installed_state, old_zypper):
    packages_to_remove = []
    for pkgspec in pkgspecs:
        # TODO: should we actually validate the packagespec to allow things like "only uninstall this specific version"?
        if installed_state[pkgspec.pkg]:
            packages_to_remove.append(pkgspec.pkg)
    if len(packages_to_remove) > 0:
        cmd = ['/usr/bin/zypper', '--non-interactive', 'remove']
        cmd.extend(packages_to_remove)

        write_debug('zypper remove command: %s' % cmd)

        if not m.check_mode:
            rc, stdout, stderr = m.run_command(cmd)
        else:
            rc = 0
            stdout = ''
            stderr = ''
            changed = True

        # TODO: bogus check- should fail on most nonzero values
        if rc == 0:
            changed=True
        else:
            changed=False
    else:
        rc = 0
        stdout = ''
        stderr = ''
        changed=False

    return (rc, stdout, stderr, changed)

def write_debug(msg):
    global debug_to_stderr, debug_messages

    if debug_to_stderr:
        sys.stderr.write(msg + '\n')
    debug_messages.append(msg)

# ===========================================
# Main control flow

def main():
    module = AnsibleModule(
        argument_spec = dict(
            name = dict(required=True, aliases=['pkg'], type='list'),
            state = dict(required=False, default='present', choices=['absent', 'installed', 'latest', 'present', 'removed']),
            disable_gpg_check = dict(required=False, default='no', type='bool'),
            disable_recommends = dict(required=False, default='yes', type='bool'),
            debug_in_result = dict(required=False, default='no', type='bool'),
            debug_in_stderr = dict(required=False, default='no', type='bool'),
        ),
        supports_check_mode = True
    )

    global debug_to_stderr

    params = module.params

    debug_to_stderr = params['debug_in_stderr']
    debug_in_result = params['debug_in_result']

    parsed_pkgspecs = [PkgSpec(p) for p in params['name']]
    state = params['state']
    disable_gpg_check = params['disable_gpg_check']
    disable_recommends = params['disable_recommends']



    rc = 0
    stdout = ''
    stderr = ''
    result = {}
    result['name'] = params['name']
    result['state'] = state

    rc, out = zypper_version(module)
    match = re.match(r'zypper\s+(\d+)\.(\d+)\.(\d+)', out)
    if not match or  int(match.group(1)) > 0:
        old_zypper = False
    else:
        old_zypper = True

    installed_versions = get_installed_versions(module, parsed_pkgspecs)

    write_debug('pre run versions:\n%s' % installed_versions)

    # Perform requested action
    if state in ['installed', 'present']:
        (rc, stdout, stderr, changed) = package_present(module, parsed_pkgspecs, installed_versions, disable_gpg_check, disable_recommends, old_zypper)
    elif state in ['absent', 'removed']:
        (rc, stdout, stderr, changed) = package_absent(module, parsed_pkgspecs, installed_versions, old_zypper)
    elif state == 'latest':
        (rc, stdout, stderr, changed) = package_latest(module, parsed_pkgspecs, installed_versions, disable_gpg_check, disable_recommends, old_zypper)

    if rc != 0:
        if stderr:
            module.fail_json(msg=stderr)
        else:
            module.fail_json(msg=stdout)

    installed_versions = get_installed_versions(module, parsed_pkgspecs)

    write_debug('post run versions: \n%s' % installed_versions)

    result['changed'] = changed
    if debug_in_result:
        result['debug_output'] = debug_messages

    module.exit_json(**result)

# import module snippets
from ansible.module_utils.basic import *
main()
