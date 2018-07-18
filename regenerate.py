#!/usr/bin/env python3

import sys
import os
import time
import argparse
import collections

import urllib.request


_REDOWNLOAD_INTERVAL = 12 * 60 * 60  # Every half of a day


_dir = os.path.dirname(os.path.realpath(__file__))
_DEFAULT_PSL = os.path.join(_dir, 'public_suffix_list.dat')
_PSL_URL = 'https://publicsuffix.org/list/public_suffix_list.dat'


def get_public_suffixes(psl_file):
    positive_public_suffixes = []
    negative_public_suffixes = []
    with open(psl_file, encoding='utf-8') as f:
        for line in f:
            line = line.split(maxsplit=1)
            if not line:
                continue
            line = line[0]
            if not line or line.startswith('//'):
                continue
            if line.startswith('!'):
                line = line[1:]
                l = negative_public_suffixes
            else:
                l = positive_public_suffixes
            l.append(line.strip('.').lower().split('.'))
    return tuple(positive_public_suffixes), tuple(negative_public_suffixes)


def need_to_redownload_psl(psl_file, current_timestamp):
    try:
        f = open(psl_file, 'rb')
    except OSError:
        return True
    else:
        try:
            if f.read(3) != b'// ':
                return True
            timestamp = f.read(20)
        finally:
            f.close()
    timestamp_end = timestamp.find(b'\n')
    if timestamp_end == -1:
        return True
    timestamp = timestamp[:timestamp_end]
    if not timestamp.isdigit():
        return True
    timestamp = int(timestamp.decode('ascii'))
    return (
        timestamp > current_timestamp + 60 * 60 or  # More than an hour in the future.
        current_timestamp - timestamp > _REDOWNLOAD_INTERVAL  # More than _REDOWNLOAD_INTERVAL has passed
    )


def redownload_psl(psl_file, current_timestamp):
    r = urllib.request.urlopen(_PSL_URL)
    header = b'// ' + str(int(current_timestamp)).encode('ascii') + b'\n'
    with open(psl_file, 'wb') as f:
        f.write(header)
        while True:
            chunk = r.read(10 * 1024)
            if not chunk:
                break
            f.write(chunk)


def rules_to_tree(rules):
    tree = collections.OrderedDict()
    for rule in rules:
        rule_tree = tree
        for label in reversed(rule):
            rule_tree = rule_tree.setdefault(label, collections.OrderedDict())
        rule_tree['!'] = True
    return tree

_CONSTANT_NAMES = {
    '!': 'accept_this',
    '*': 'wildcard'
}

def to_swift_string(s):
    return _CONSTANT_NAMES.get(s, None) or '"{}"'.format(s)

def get_name(prefix, s, cache):
    name = cache.get(s, None)
    if name is None:
        cache[s] = name = '__AUTOGEN_' + prefix + '_' + str(len(cache))
    return name

def make_swift_dict(name, d, callback, *, _cache=None, __prefix=None):
    if _cache is None:
        _cache = {}
    if __prefix is None:
        __prefix = name
    for k, v in d.items():
        if isinstance(v, dict) and v != {'!': True}:
            make_swift_dict(get_name(__prefix, name + k, _cache), v, callback, _cache=_cache, __prefix=__prefix)
    callback('fileprivate let ' + name + ' = Rule.subrule([')
    first = True
    for k, v in d.items():
        if isinstance(v, dict):
            if v == {'!': True}:
                sub_name = '_accept_this_dict'
            else:
                sub_name = get_name(__prefix, name + k, _cache)
        elif v is True:
            sub_name = 'Rule.accept_this'
        else:
            raise RuntimeError
        if first:
            first = False
            callback('\n    ')
        else:
            callback(',\n    ')
        callback(to_swift_string(k) + ': ' + sub_name)
    callback('\n])\n')


def filter_top_rules(d):
    # If only a single label top level
    # domain is accepted, remove it as
    # it is covered by the implicit `*` rule.
    return {k: v for k, v in d.items() if v != {'!': True}}

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(prog='regenerate_public_suffix_list')
    parser.add_argument(metavar='SWIFT FILE', dest='swift_file', help='.swift file to write to')
    parser.add_argument(metavar='PUBLIC SUFFIX LIST', dest='psl_file', help='The public suffix list. Defaults to "public_suffix_list.dat" in the same directory as this python file.', default=_DEFAULT_PSL, nargs='?')

    args = parser.parse_args(argv)

    swift_file = args.swift_file
    psl_file = args.psl_file

    current_timestamp = int(time.time())

    if need_to_redownload_psl(psl_file, current_timestamp):
        print('Redownloading public suffix list.')
        redownload_psl(psl_file, current_timestamp)
        print('Done downloading.')
    else:
        print('Not redownloading public suffix list.')
        if os.path.exists(swift_file):
            print('Not regenerating swift file as it already exits.')
            sys.exit(1)
            return

    print('Reading public suffix list file.')
    positive_public_suffixes, negative_public_suffixes = get_public_suffixes(psl_file)

    print('Regenerating swift file.')

    with open(swift_file, 'w', encoding='utf-8') as f:
        f.write(
'''
// Autogenerated at %%timestamp%%

import Foundation

fileprivate enum Rule {
    case accept_this
    case subrule([String: Rule])

    var subrule: [String: Rule] {
        get {
            switch self {
                case .subrule(let value): return value
                case .accept_this: assert(false)
            }
        }
    }
}

fileprivate let accept_this = "!"
fileprivate let wildcard = "*"

fileprivate let _accept_this_dict = Rule.subrule([
    accept_this: Rule.accept_this
])

// This section is compiled from the public suffix list.
// TEMPLATE SECTION

'''.strip('\n').replace('%%timestamp%%', str(current_timestamp)) + '\n\n'

            )

        # f.write('fileprivate let p_suffixes = Rule.subrule(')
        make_swift_dict('p_suffixes', filter_top_rules(rules_to_tree(positive_public_suffixes)), f.write)
        # f.write(')\n\nfileprivate let n_suffixes = Rule.subrule(')
        f.write('\n\n')
        make_swift_dict('n_suffixes', filter_top_rules(rules_to_tree(negative_public_suffixes)), f.write)
        # f.write(')\n\n')
        f.write('\n\n')

        f.write(
'''
// END TEMPLATE SECTION

func get_domain(_ hostname: String) -> String? {
    let labels = Array<String>(hostname.components(separatedBy: ".").reversed())

    var longest: Array<String> = Array<String>()
    var this_longest: Array<String> = Array<String>()

    // Check negative rules

    var accepted_rules: Array<Rule> = [n_suffixes]


    for label in labels {
        if (label.count == 0) {
            return nil
        }
        var new_accepted_rules: Array<Rule> = Array<Rule>()
        for rule in accepted_rules {
            if (rule.subrule[accept_this] != nil) {
                longest = this_longest
            }
            if let wildcard_rule = rule.subrule[wildcard] {
                new_accepted_rules.append(wildcard_rule)
            }
            if let label_rule = rule.subrule[label] {
                new_accepted_rules.append(label_rule)
            }
        }
        this_longest.append(label)
        accepted_rules = new_accepted_rules
    }

    if (longest.count != 0) {
        return longest.reversed().joined(separator: ".")
    }

    // Check positive rules

    longest = []
    this_longest = []

    accepted_rules = [p_suffixes]

    for label in labels {
        this_longest.append(label)
        var new_accepted_rules = Array<Rule>()
        for rule in accepted_rules {
            if (rule.subrule[accept_this] != nil) {
                longest = this_longest
            }
            if let wildcard_rule = rule.subrule[wildcard] {
                new_accepted_rules.append(wildcard_rule)
            }
            if let label_rule = rule.subrule[label] {
                new_accepted_rules.append(label_rule)
            }
        }
        accepted_rules = new_accepted_rules
    }

    for rule in accepted_rules {
        if (rule.subrule[accept_this] != nil) {
            // Given domain is a TLD. e.g. "com" or "co.uk" was given.
            return nil
        }
    }

    if (longest.count != 0) {
        return longest.reversed().joined(separator: ".")
    }

    // If no rules match, the rule is just "*".

    if (labels.count < 2) {
        return nil
    }

    return labels[1] + "." + labels[0]
}

'''.strip('\n') + '\n\n'
            )


if __name__ == '__main__':
    main()
