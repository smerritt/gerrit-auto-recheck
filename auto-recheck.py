#!/usr/bin/env python

import json
import operator
import os
import re
import subprocess


GERRIT_SERVER = "review.openstack.org"
GERRIT_PORT = 29418

SEARCH = ("status:open AND "
          "(project:openstack/swift OR project:openstack/swift-bench OR project:openstack/python-swiftclient) AND "
          "(label:Verified=-1 OR label:Verified=-2) AND NOT label:WorkFlow=-1")


def fetch_failed_reviews():
    gerrit_cmd = ['ssh', '-p', str(GERRIT_PORT), GERRIT_SERVER, 'gerrit', 'query',
                  '--format=JSON', '--comments', '--current-patch-set']

    gerrit_process = subprocess.Popen(
        gerrit_cmd + SEARCH.split(),
        stdout=subprocess.PIPE)

    output, _ = gerrit_process.communicate()

    lines = [line for line in output.splitlines() if line]
    lines.pop()  # last record is query stats; we don't care
    reviews = [json.loads(line) for line in lines]
    reviews.sort(key=operator.itemgetter('url'))
    return reviews


def should_ignore_review(review):
    # OpenStack Proposal Bot just does the global requirements stuff, and
    # nobody cares.
    return review['owner']['username'] == 'proposal-bot'


def is_flaky_job(job_name):
    return (job_name.startswith("check-tempest-") or
            job_name.startswith("check-devstack-") or
            job_name.startswith("gate-tempest-") or
            job_name.startswith("gate-devstack-"))


def extract_jobs_from_ci_message(comment):
    """
    Returns the status and name of jobs from the last CI run.

    :param comment: text of the review comment (a string)
    :returns: 2-tuple: (list of failed jobs, list of successful jobs)

    Non-voting jobs are ignored.

    Example comment:

Patch Set 4: Doesn't seem to work

Build failed.  For information on how to proceed, see https://wiki.openstack.org/wiki/GerritJenkinsGit#Test_Failures

- gate-swift-pep8 http://logs.openstack.org/68/89568/4/check/gate-swift-pep8/450dd07 : SUCCESS in 1m 35s
- gate-swift-docs http://docs-draft.openstack.org/68/89568/4/check/gate-swift-docs/49cfa85/doc/build/html/ : SUCCESS in 2m 26s
- gate-swift-python26 http://logs.openstack.org/68/89568/4/check/gate-swift-python26/b067109 : SUCCESS in 3m 01s
- gate-swift-python27 http://logs.openstack.org/68/89568/4/check/gate-swift-python27/17404b3 : SUCCESS in 2m 40s
- check-tempest-dsvm-full http://logs.openstack.org/68/89568/4/check/check-tempest-dsvm-full/93b669f : SUCCESS in 58m 15s
- check-tempest-dsvm-neutron http://logs.openstack.org/68/89568/4/check/check-tempest-dsvm-neutron/d0e0b17 : SUCCESS in 53m 08s
- check-tempest-dsvm-neutron-heat-slow http://logs.openstack.org/68/89568/4/check/check-tempest-dsvm-neutron-heat-slow/86b349d : SUCCESS in 21m 48s (non-voting)
- check-grenade-dsvm http://logs.openstack.org/68/89568/4/check/check-grenade-dsvm/4910770 : SUCCESS in 35m 33s
- check-grenade-dsvm-neutron http://logs.openstack.org/68/89568/4/check/check-grenade-dsvm-neutron/6bf8780 : FAILURE in 26m 44s (non-voting)
- check-swift-dsvm-functional http://logs.openstack.org/68/89568/4/check/check-swift-dsvm-functional/bdc0787 : FAILURE in 13m 53s
- check-devstack-dsvm-cells http://logs.openstack.org/68/89568/4/check/check-devstack-dsvm-cells/bee481a : SUCCESS in 11m 52s
- check-tempest-dsvm-postgres-full http://logs.openstack.org/68/89568/4/check/check-tempest-dsvm-postgres-full/265befe : SUCCESS in 1h 03m 30s
- gate-tempest-dsvm-large-ops http://logs.openstack.org/68/89568/4/check/gate-tempest-dsvm-large-ops/e55f63d : SUCCESS in 15m 33s
- gate-tempest-dsvm-neutron-large-ops http://logs.openstack.org/68/89568/4/check/gate-tempest-dsvm-neutron-large-ops/db9d023 : SUCCESS in 16m 35s


    The result will be (['check-grenade-dsvm-neutron', 'check-swift-dsvm-functional'],
                        ['gate-swift-pep8', 'gate-swift-docs', 'gate-swift-python26', 'gate-swift-python27',
                         'check-tempest-dsvm-full', 'check-tempest-dsvm-neutron',
                         'check-grenade-dsvm', 'check-devstack-dsvm-cells',
                         'check-devstack-dsvm-postgres-full', 'gate-tempest-dsvm-large-ops',
                         'gate-tempest-dsvm-neutron-large-ops']).

    Note that "check-grenade-dsvm-neutron" and "check-tempest-dsvm-neutron-heat-slow" do not appear in the output;
    they are non-voting.
    """
    job_status = re.compile("- (\S+) http://\S+ : (SUCCESS|FAILURE)")

    successes = []
    failures = []

    for line in comment.splitlines():
        if "(non-voting)" in line:
            continue
        match = re.match(job_status, line)
        if not match:
            continue

        job, status = match.groups()

        (successes if status == "SUCCESS" else failures).append(job)

    return (failures, successes)


def extract_bug_number_from_er_message(comment):
    """
    Return the bug number that Elastic Recheck thinks broke the build.

    If multiple bug numbers are present, arbitrarily pick one.
    """
    # XXX write me
    return None


def retry_with(review):
    """
    How to retry a particular bug: returns the string to post in a comment to
    trigger an appropriate recheck.

    :returns: None if no recheck needed,
              "recheck no bug", or "recheck bug <N>".
    """

    comments = review.get('comments', [])
    if not comments:
        return None

    # Either the last review comment is Jenkins *or* the second-to-last
    # comment is Jenkins(CI) and the last one is Elastic Recheck.
    ci_comment = er_comment = None
    if comments[-1]['reviewer']['username'] == 'jenkins':
        ci_comment = comments[-1]
    elif (comments[-1]['reviewer']['username'] == 'elasticrecheck'
          and comments[-2]['reviewer']['username'] == 'jenkins'):
        er_comment = comments[-1]
        ci_comment = comments[-2]
    else:
        return None

    failed_jobs, successful_jobs = extract_jobs_from_ci_message(
        ci_comment['message'])

    # It is highly atypical that every job would fail, so let's fail safe.
    if not successful_jobs:
        return None

    # Something not flaky failed? Better not spam the world.
    if not all(is_flaky_job(j) for j in failed_jobs):
        return None

    comment = "recheck no bug"
    if er_comment:
        bug = extract_bug_number_from_er_message(er_comment['message'])
        if bug:
            comment = "recheck bug %s" % bug
    return comment


def post_comment(review_id, comment):
    gerrit_cmd = ['ssh', '-p', str(GERRIT_PORT), GERRIT_SERVER, 'gerrit',
                  'review', '--message', '"%s"' % comment, review_id]
    subprocess.check_call(gerrit_cmd)


def main():
    did_something = False
    for review in fetch_failed_reviews():
        retry_comment = retry_with(review)
        if retry_comment is not None:
            if should_ignore_review(review):
                continue
            did_something = True
            print "%s -> %s" % (review['url'], retry_comment)

            should_post_comment = (
                os.environ.get('AUTO_RECHECK_POST', 'no').lower()
                in ('true', '1', 'yes', 'on', 't', 'y'))
            if should_post_comment:
                post_comment(review['currentPatchSet']['revision'],
                             retry_comment)
    if not did_something:
        print "No reviews need rechecks."


if __name__ == '__main__':
    main()
