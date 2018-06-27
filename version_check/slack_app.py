# -*- coding: utf-8 -*-
'''
Main file that runs a tornado server for a Version Check Slack App.

This file also contains documentation for setting up and running the Slack App.

Dependencies
============

- Python 3.6
- Tornado

Installation
============

There are 3 parts to set up in order use version_check as a Slack App:

- Slack Slash Command
- Tornado Server (this file)
- Git Clone + Fetch Management

Slack Application
-----------------

The first step is installing a Slack App Slash Command in the team workspace the
Version Check program will run in.

.. note::
    You will need admin access to the Slack team you're adding the App to.

To do so, the following settings should be established:

1. Create a [Slack App](https://api.slack.com/apps?new_app=1)
2. Name the App. For example "My-Repo Version Check"
3. Choose the Development Workspace (if applicable).
4. Click "Create App"
5. Under "Add features and functionality", choose "Slash Commands", then click
   "Create New Command"
6. Fill out the related fields. This app presently accepts only one option, which
   is the PR number to search for. This may be expanded in the future, but for now,
   create your Slack App with this information in mind. Note also that this file
   uses "/version-check" as the event endpoint to send the requests.
7. Once the App is created, note the "Verification Token". That will be used later.
8. Install the App in your Slack team.

For more information about configuring and installing a slash command, please see
Slack's [Slash Command Documentation](https://api.slack.com/slash-commands).

Tornado Server
--------------

The second step is running the Tornado server (this file). The slack_app.py server
receives POST requests from Slack, runs the search, and responds to Slack with the
results.

Two environment variables must be set before running the server:

- SLACK_TOKEN
- SLACK_TEAM_ID

The ``SLACK_TOKEN`` variable is the "Verification Token" that is generated when
the app is installed in the Slack.

The ``SLACK_TEAM_ID`` is an additional measure used to verify that the POST
request is coming from the correct Slack team. The easiest way to find this ID is
to login to the team in a web browser, right click on the page, and select "View
Page Source". Then, search for "team_id".

Once the environment variables are in place, run the file to start the server in
the foreground:

.. code-block:: bash

    python3 slack_app.py

Some settings can be changed in the ``config`` file in this repo, such as the
location of the git clone, the port the server should run on (defaults to 8888),
or the name of the git clone's remote.

Git Clone & Cron Job
--------------------

The final part is setting up a git clone of the selected repo and a management job
of some kind configured to fetch upstream contributions, such as a cron job.

.. note::
    The git repository should be cloned to the server running the tornado server
    _before_ starting the ``slack_app.py`` file.

Once the repository is cloned, set up a management job to periodically pull down
any new changes to the repo. This ensures the results of the version_check search
are up-to-date.
'''

# Import Python libs
import logging
import json
import os
import sys
import urllib.parse

# Import tornado libs
from tornado import gen
import tornado.ioloop
import tornado.web
import tornado.httpclient

# Import Version Check libs
import config
import core

SLACK_TOKEN = os.environ.get('SLACK_TOKEN')
SLACK_TEAM_ID = os.environ.get('SLACK_TEAM_ID')

LOG = logging.getLogger(__name__)


class EventHandler(tornado.web.RequestHandler):
    '''
    Main handler for the ``/salt-version`` endpoint
    '''

    def data_received(self, chunk):
        pass

    @gen.coroutine
    def post(self, *args, **kwargs):
        params = urllib.parse.parse_qs(
            self.request.body.decode()
        )

        if not _validate_slack_signature(self.request, params):
            raise tornado.web.HTTPError(401)

        url = params.get('response_url')[0]
        try:
            pr_num = params.get('text')[0]
        except TypeError:
            LOG.error('PR number was not provided.')
            post_data = {'attachments': [{'text': 'Please provide a PR number.',
                                          'color': 'danger'}]}
            yield api_call(url, post_data)
            return

        pr_num = pr_num.lstrip('#')

        # Respond immediately to slack (no results yet)
        yield api_call(url, {'text': 'PR #{0} Results:'.format(pr_num)})

        # Find matches; longer running job
        yield get_matches(url, pr_num)
        return


def make_app():
    '''
    Create the tornado web application - uses the "events" endpoint.
    '''
    return tornado.web.Application([
        ('/salt-version', EventHandler),
    ])


@gen.coroutine
def api_call(url, post_data):
    '''
    Send a POST request to Slack.

    url
        The URL to send the api call to.

    post_data
        The data to send to Slack.
    '''
    http_client = tornado.httpclient.AsyncHTTPClient()
    request = tornado.httpclient.HTTPRequest(
        url=url,
        method='POST',
        headers={'Content-Type': 'application/json'},
        body=json.dumps(post_data).encode('utf-8')
    )

    yield http_client.fetch(request)
    return


@gen.coroutine
def get_matches(url, pr_num):
    '''
    Search for the branches and tags that the PR is included in, then format those
    matches into the correct post_data, and reply to Slack.

    url
        The URL to respond to.

    pr_num
        The PR number to search for.
    '''
    post_data = {}
    LOG.info('PR #%s: Searching for matches.', pr_num)

    # Find any branch or tag matches
    matches = core.search(pr_num)
    branches = matches.get('branches')
    tags = matches.get('tags')

    # Configure matches in respective "fields"
    fields = []
    if branches:
        branches = ", ".join(branches)
        fields.append({'title': 'Branches', 'value': branches})
    if tags:
        tags = ", ".join(tags)
        fields.append({'title': 'Tags', 'value': tags})

    if fields:
        # We have matches, format attachment fields
        LOG.info('PR #%s: Matches found: %s', pr_num, fields)
        post_data['attachments'] = [{'fields': fields,
                                     'color': 'good'}]
    else:
        # No matches found, set default message
        LOG.info('PR #%s: No matches found.', pr_num)
        post_data['attachments'] = [
            {'text': 'No matches found for PR #{0}'.format(pr_num),
             'color': 'warning'}]

    # Respond to Slack with results
    yield api_call(url, post_data)
    return


def _validate_slack_signature(request, params):
    '''
    Validate that the request is coming from Slack.

    request
        The incoming request to validate

    params
        The incoming request's body parameters
    '''
    name, version, url = request.headers.get('User-Agent').split()
    if name != 'Slackbot' or url != '(+https://api.slack.com/robots)':
        return False

    if params.get('token')[0] != SLACK_TOKEN \
            and params.get('team_id')[0] != SLACK_TEAM_ID:
        return False

    LOG.info('Received Version Check event from slack. Processing...')
    return True


def _check_env_vars():
    '''
    Make sure all environment variables are set before starting app.
    '''
    check_ok = True

    if SLACK_TOKEN is None:
        check_ok = False
        LOG.error(
            'Version Check was started without a Slack Token. '
            'Please set the SLACK_TOKEN environment variable.'
        )

    if SLACK_TEAM_ID is None:
        check_ok = False
        LOG.error(
            'Version Check was started without a Slack Team ID. '
            'Please set the SLACK_TEAM_ID environment variable.'
        )

    return check_ok


def _setup_logging():
    '''
    Set up the logging files needed to run the app.
    '''
    log_path = '/var/log/version_check/version_check.log'

    # Check if logging directory exists and attempt to create it if necessary
    log_dir = os.path.dirname(log_path)
    if not os.path.exists(log_dir):
        LOG.info('Log directory not found. Trying to create it: %s', log_dir)
        try:
            os.makedirs(log_dir, mode=0o700)
        except OSError as err:
            LOG.error('Failed to create directory for log file: %s (%s)',
                      log_dir, err)
            return

    # Set the log level, if provided. Otherwise, default to INFO
    log_level = os.environ.get('LOG_LEVEL', '').upper()
    if log_level:
        numeric_level = getattr(logging, log_level, None)
        if not isinstance(numeric_level, int):
            LOG.error('Invalid log level: %s', log_level)
            return
    else:
        log_level = logging.INFO

    # Set up the basic logger config
    logging.basicConfig(
        filename=log_path,
        format='[%(levelname)s] %(message)s',
        level=log_level
    )

    # Add a StreamHandler to the logger to also stream logs to the console
    console = logging.StreamHandler()
    console.setLevel(log_level)
    logging.getLogger('').addHandler(console)


if __name__ == '__main__':
    # First, set up logging.
    _setup_logging()

    # Check for mandatory settings.
    if _check_env_vars() is False:
        sys.exit()

    LOG.info('Starting Version Check server.')
    LOG.info('Listening on port \'%s\'.', config.SLACK_APP_PORT)

    APP = make_app()
    APP.listen(config.SLACK_APP_PORT)
    tornado.ioloop.IOLoop.current().start()
