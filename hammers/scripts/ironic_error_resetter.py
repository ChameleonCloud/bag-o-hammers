# coding: utf-8
'''
Basic Usage:

.. code-block:: bash

    ironic-error-resetter {info, reset}

Resets Ironic nodes in error state with a known, common error. Started out
looking for IPMI-related errors, but isn't intrinsically specific to them
over any other error that shows up on the nodes. Records the resets it
performs on the node metadata (``extra`` field) and refuses after some number
(currently 3) of accumulated resets.

Currently watches out for:

.. code-block:: text
    ^Failed to tear down

'''


import datetime
import os
import re
import sys
import time
# import json
# from pprint import pprint

from dateutil.tz import tzutc
import requests

from hammers import osrest
from hammers.osapi import load_osrc, Auth
from hammers.slack import Slackbot
from hammers.util import error_message_factory, base_parser

OS_ENV_PREFIX = 'OS_'
SUBCOMMAND = 'ironic-error-resetter'

ERROR_MATCHERS = [re.compile(r) for r in [
    r'^Failed to tear down',
]]

_thats_crazy = error_message_factory(SUBCOMMAND)

def cureable_nodes(nodes_details):
    bad_nodes = [
        nid
        for (nid, n)
        in nodes_details.items()
        if (
            not n['maintenance'] and
            n['provision_state'] == 'error' and
            any(ro.search(str(n['last_error'])) for ro in ERROR_MATCHERS)
        )
    ]
    return bad_nodes


class NodeEventTracker(object):
    """
    Tracks events by putting timestamps on the Ironic node "extra" metadata
    field.
    """
    def __init__(self, auth, node_id, extra_key):
        self.auth = auth
        self.nid = node_id
        self.extra_key = extra_key
        self._update()

    def __repr__(self):
        return '<{}: {}>'.format(
            self.__class__.__name__,
            self.auth.endpoint('baremetal') + '/v1/nodes/{}'.format(self.nid),
        )

    def _update(self):
        self.node = osrest.ironic_node(self.auth, self.nid)

    def mark(self):
        """Add new timestamp"""
        now = datetime.datetime.now(tz=tzutc()).isoformat()
        if self.count() != 0:
            path = '/extra/{}/-'.format(self.extra_key)
            value = now
        else:
            path = '/extra/{}'.format(self.extra_key)
            value = [now]

        patch = [{
            'op': 'add',
            'path': path,
            'value': value,
        }]
        response = requests.patch(
            url=self.auth.endpoint('baremetal') + '/v1/nodes/{}'.format(self.nid),
            headers={
                'X-Auth-Token': self.auth.token,
                'X-OpenStack-Ironic-API-Version': '1.9',
            },
            json=patch,
        )
        response.raise_for_status()
        self.node = response.json()

    def count(self):
        """Count number of timestamps"""
        return len(self.node['extra'].get(self.extra_key, []))

    def clear(self):
        """Clear timestamps"""
        if self.count() == 0:
            return

        patch = [{
            'op': 'remove',
            'path': '/extra/{}'.format(self.extra_key),
        }]
        response = requests.patch(
            url=self.auth.endpoint('baremetal') + '/v1/nodes/{}'.format(self.nid),
            headers={
                'X-Auth-Token': self.auth.token,
                'X-OpenStack-Ironic-API-Version': '1.9',
            },
            json=patch,
        )
        response.raise_for_status()
        self.node = response.json()


class NodeResetter(object):
    extra_key = 'hammer_error_resets'

    def __init__(self, auth, node_id, dry_run=False):
        self.auth = auth
        self.nid = node_id
        self.tracker = NodeEventTracker(auth, node_id, extra_key=self.extra_key)
        self.dry_run = dry_run

    # Pass-thru to the tracker's node data dictionary. Note: doing a basic
    # self.node = self.tracker.node would work until tracker reassigns the
    # entire object, which it does often.
    @property
    def node(self):
        return self.tracker.node
    # @node.setter
    # def node(self, value):
    #     self.tracker.node = value

    def reset(self):
        if not self.dry_run:
            self.tracker.mark()

        for n in range(3):
            # try a few times because quickly sending a state transition and
            # patching the extra field can raise a 409.
            time.sleep(n + 1)
            try:
                if not self.dry_run:
                    self._reset()
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 409:
                    continue # retry
                else:
                    raise # die
            else:
                break # done

    def _reset(self):
        osrest.ironic_node_set_state(self.auth, self.nid, 'deleted')


def main(argv=None):
    if argv is None:
        argv = sys.argv

    parser = base_parser(
        'Kick Ironic nodes that are in an common/known error state')
    parser.add_argument('mode', choices=['info', 'reset'],
        help='Just display data on the stuck nodes or reset their states')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--dry-run', action='store_true',
        help='Dry run, don\'t actually do anything')

    args = parser.parse_args(argv[1:])

    slack = Slackbot(args.slack, script_name='ironic-error-resetter') if args.slack else None

    os_vars = {k: os.environ[k] for k in os.environ if k.startswith(OS_ENV_PREFIX)}
    if args.osrc:
        os_vars.update(load_osrc(args.osrc))
    missing_os_vars = set(Auth.required_os_vars) - set(os_vars)
    if missing_os_vars:
        print(
            'Missing required OS values in env/rcfile: {}'
            .format(', '.join(missing_os_vars)),
            file=sys.stderr
        )
        return -1

    auth = Auth(os_vars)

    try:
        nodes = osrest.ironic_nodes(auth, details=True)
        cureable = cureable_nodes(nodes)

        if args.mode == 'info':
            print('{} node(s) in a state that we can treat'.format(len(cureable)))
            for nid in cureable:
                print('-' * 40)
                print('\n'.join(
                    '{:<25s} {}'.format(key, nodes[nid].get(key))
                    for key
                    in [
                        'uuid',
                        'provision_updated_at',
                        'provision_state',
                        'last_error',
                        'instance_uuid',
                        'extra',
                        'maintenance',
                    ]
                ))
            return

        if len(cureable) == 0:
            if args.verbose:
                print('Nothing to do.')
            return

        print('To correct: {}'.format(repr(cureable)))

        reset_ok = []
        too_many = []
        for nid in cureable:
            resetter = NodeResetter(auth, nid, dry_run=args.dry_run)
            try:
                resetter.reset()
                reset_ok.append((nid, resetter.tracker.count()))

        message_lines = []
        if reset_ok:
            message_lines.append('Performed reset of nodes')
            message_lines.extend(' • `{}`: {} resets'.format(*r) for r in reset_ok)
        if too_many:
            message_lines.append('Skipped (already at limit)')
            message_lines.extend(' • `{}`'.format(r) for r in too_many)
        if args.dry_run:
            message_lines.append('dry run, no changes actually made.')

        message = '\n'.join(message_lines)

        print(message)

        if slack and (not args.dry_run):
            slack.success(message)
    except:
        if slack:
            slack.exception()
        raise


if __name__ == '__main__':
    sys.exit(main(sys.argv))
