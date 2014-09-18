#!/usr/bin/env python -u

import argparse
import json
import logging
import operator
import os
import re
import subprocess
import sys
import time


GERRIT_SERVER = "review.openstack.org"
GERRIT_PORT = 29418
MINIMUM_REVIEW_AGE = 15 * 60  # 15 minutes

SEARCH = ("status:open AND "
          "(project:openstack/swift OR project:openstack/swift-bench OR project:openstack/python-swiftclient) AND "
          "(label:Verified=-1 OR label:Verified=-2) AND NOT label:WorkFlow=-1")


def fetch_failed_reviews():
    gerrit_cmd = ['ssh', '-p', str(GERRIT_PORT), GERRIT_SERVER, 'gerrit', 'query',
                  '--format=JSON', '--comments', '--current-patch-set']
    gerrit_cmd.extend(SEARCH.split())

    logging.debug("Running %s", ' '.join(gerrit_cmd))
    gerrit_process = subprocess.Popen(gerrit_cmd, stdout=subprocess.PIPE)

    output, _ = gerrit_process.communicate()
    logging.debug("SSH complete")

    lines = [line for line in output.splitlines() if line]
    lines.pop()  # last record is query stats; we don't care
    reviews = [json.loads(line) for line in lines]
    reviews.sort(key=operator.itemgetter('url'))
    return reviews


def should_ignore_review(review):
    """
    Whether or not a review should be ignored. This is all pretty specific
    to OpenStack Swift.
    """
    # Let's not drag up the long-dead past.
    if int(review['number']) < 100000:
        logging.debug("  Ignoring review because its number (%d) is too small",
                      int(review['number']))
        return True

    # If there's a code-review -2 on it, then no amount of automatic
    # rechecking will make people happy.
    code_reviews = [int(ap['value'])
                    for ap in review['currentPatchSet']['approvals']
                    if ap['type'] == 'Code-Review']
    if -2 in code_reviews:
        logging.debug("  Ignoring review due to -2")
        return True

    # OpenStack Proposal Bot just does the global requirements stuff, and
    # nobody cares.
    if review['owner']['username'] == 'proposal-bot':
        logging.debug("  Ignoring review because it's from proposal-bot")
        return True

    # Anything with a failure less than $MINIMUM_REVIEW_AGE seconds old
    # should wait to give Elastic Recheck a chance to do its thing.
    if time.time() - review['comments'][-1]['timestamp'] <= MINIMUM_REVIEW_AGE:
        logging.debug("  Ignoring review because it's too new")
        return True


def is_flaky_job(job_name):
    return (job_name.startswith("check-tempest-") or
            job_name.startswith("check-devstack-") or
            job_name.startswith("gate-tempest-") or
            job_name.startswith("gate-devstack-") or
            job_name.startswith("check-grenade-") or
            job_name.startswith("gate-grenade-"))


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
    job_status = re.compile("- (\S+) http://\S+ : (\S+)")

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
    # Sample ER comment: u"Patch Set 8:
    #
    # I noticed jenkins failed, I think you hit bug(s):
    #
    # - check-tempest-dsvm-neutron-full: https://bugs.launchpad.net/bugs/1357476 https://bugs.launchpad.net/bugs/1254890
    #
    # If you believe we've correctly identified the failure, feel free to leave a 'recheck' comment to run the tests again.
    # For more details on this and other bugs, please see http://status.openstack.org/elastic-recheck/"
    bug_link = re.compile("https://bugs.launchpad.net/bugs/(\d+)")
    match = re.search(bug_link, comment)
    return int(match.group(1)) if match else None


def retry_with(review):
    """
    How to retry a particular bug: returns the string to post in a comment to
    trigger an appropriate recheck.

    :returns: None if no recheck needed,
              "recheck no bug", or "recheck bug <N>".
    """

    comments = review.get('comments', [])
    if not comments:
        logging.debug("  No review comments")
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
        logging.debug("  No CI comment in last 2 review comments")
        return None

    failed_jobs, successful_jobs = extract_jobs_from_ci_message(
        ci_comment['message'])

    # It is highly atypical that every job would fail, so let's fail safe.
    if not successful_jobs:
        logging.debug("  No successful jobs at all")
        return None

    # Something not flaky failed? Better not spam the world.
    if not all(is_flaky_job(j) for j in failed_jobs):
        non_flaky_jobs = [j for j in failed_jobs if not is_flaky_job(j)]
        logging.debug("  Non-flaky jobs failed (%s)",
                      ', '.join(non_flaky_jobs))
        return None

    comment = "recheck no bug"
    if er_comment:
        logging.debug("  Found Elastic Recheck comment")
        bug = extract_bug_number_from_er_message(er_comment['message'])
        if bug:
            comment = "recheck bug %d" % bug
    else:
        logging.debug("  No Elastic Recheck comment found")

    return comment


def post_comment(review_id, comment):
    gerrit_cmd = ['ssh', '-p', str(GERRIT_PORT), GERRIT_SERVER, 'gerrit',
                  'review', '--message', '"%s"' % comment, review_id]
    subprocess.check_call(gerrit_cmd)
    logging.info("  Comment posted.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='store_true',
                        default=False, help="Verbose output")
    parser.add_argument('-p', '--post', action='store_true',
                        default=False, help="Post 'recheck no bug' comments")
    args = parser.parse_args()

    # Get logging set up either verbosely or not
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    root_logger.addHandler(handler)

    did_something = False
    for review in fetch_failed_reviews():
        logging.debug("Considering review %s", review['url'])
        retry_comment = retry_with(review)
        if retry_comment is not None:
            if should_ignore_review(review):
                continue
            did_something = True
            logging.info("%s -> %s", review['url'], retry_comment)

            if args.post:
                logging.debug("  Going to post comment %s", retry_comment)
                post_comment(review['currentPatchSet']['revision'],
                             retry_comment)
    if not did_something:
        print "No reviews need rechecks."


if __name__ == '__main__':
    main()
