# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Contributors: Guillaume Destuynder <gdestuynder@mozilla.com>

from flask import Flask, request, session, jsonify, render_template
from flask_session import Session
from flask_assets import Environment, Bundle

import logging
import mimetypes
import os
import subprocess
import sys
import time

import config

app = Flask(__name__)
app.config.from_object(config.Config(app).settings)
app.logger.addHandler(logging.StreamHandler(sys.stdout))
app.logger.setLevel(app.config.get('LOG_LEVEL'))
Session(app)

assets = Environment(app)
js = Bundle('js/base.js', filters='jsmin', output='js/gen/packed.js')
assets.register('js_all', js)
sass = Bundle('css/base.scss', filters='scss')
css = Bundle(sass, filters='cssmin', output='css/gen/all.css')
assets.register('css_all', css)
# Required to load svg
mimetypes.add_type('image/svg+xml', '.svg')

def verify_user_ca(user_ca='./scripts/user_ca_key'):
    SSH_GEN_CA_SCRIPT = './scripts/02_gen_client_ca.sh'

    if (os.path.isfile(user_ca) or os.path.isfile(user_ca+'.pub')):
        return True
    ecode = subprocess.call([SSH_GEN_CA_SCRIPT])
    app.logger.debug('Ran SSH_GEN_CA_SCRIPT exit code is {}'.format(ecode))
    if ecode != 0:
        return True
    return False

def load_session_hack(cli_token):
    """
    Loads a session manually for a certain cli_token
    """
    # Attempt to load local session - this only works for file system sessions
    # See https://github.com/pallets/werkzeug/blob/master/werkzeug/contrib/cache.py for format
    # XXX FIXME replace this by a custom session handler for Flask
    from werkzeug.contrib.cache import FileSystemCache
    import pickle
    cache = FileSystemCache(app.config['SESSION_FILE_DIR'],
                            threshold=app.config['SESSION_FILE_THRESHOLD'],
                            mode=app.config['SESSION_FILE_MODE'])
    found = False
    for fn in cache._list_dir():
        with open(fn, 'rb') as f:
            fot = pickle.load(f)
            del fot # Unused
            local_session = pickle.load(f)
            if type(local_session) is not dict:
                continue
            if local_session.get('cli_token') == cli_token:
                found = True
                break
    if not found:
        return None
    return local_session


def verify_cli_token(cli_token, session=session):
    """
    Check the cli_token provided is valid and matches the HTTP session, else clear session / bail
    """
    if (session.get('cli_token')):
        if (cli_token != session.get('cli_token')):
            app.logger.error('User with same session provided a new cli_token, destroying session')
            session.clear()
            return False
        return True
    else:
        app.logger.error('No cli_token found')
        return False


@app.route('/', methods=['GET'])
def main():
    """
    Expects ?type=ssh&host={}&port={}&cli_token={}
    """

    # GET parameter set up
    required_params = ['type', 'user', 'host', 'port', 'cli_token']
    params = request.args.to_dict()
    if set(required_params) != set(params.keys()):
        return render_template('denied.html', reason='Incorrect GET parameters'), 403
    cli_token = params.get('cli_token')
    ssh_host = params.get('host')
    ssh_port = params.get('port')
    ssh_user = params.get('user')

    # Reverse OIDC Proxy headers set up
    required_headers = ['X-Forwarded-User', 'X-Forwarded-Groups']
    if not set(required_headers).issubset(request.headers.keys()):
        return render_template('denied.html', reason='Incorrect HEADERS'), 403
    user = request.headers.get('X-Forwarded-User')
    groups = request.headers.get('X-Forwarded-Groups')

    # Session set up
    session['username'] = user
    session['groups'] = groups
    session['ssh_user'] = ssh_user
    session['ssh_port'] = ssh_port
    session['ssh_host'] = ssh_host

    if (not session.get('cli_token')):
        session['cli_token'] = cli_token
    else:
        if not verify_cli_token(cli_token):
            return render_template('denied.html', reason='cli token verification failure'), 403
    # Reverse proxy cookie
    ap_session = request.cookies.get(app.config.get('REVERSE_PROXY_COOKIE_NAME'))

    session['ap_session'] = ap_session

    app.logger.info('New user logged in {} (sid {} ap_session {}) requesting access to {}:{}'.format(user,
                                                                                                     session.sid,
                                                                                                     ap_session,
                                                                                                     ssh_host,
                                                                                                     ssh_port))
    app.logger.debug(str(ap_session))
    return render_template('main.html')

@app.route('/api/session', methods=['GET'])
def api_session():
    """
    Used to return the reverse proxy cookie for the cli client to authenticate
    """
    cli_token = request.args.get('cli_token')
    response = {'cli_token_authenticated': False,
                'ap_session': ''}

    local_session = load_session_hack(cli_token)
    if not local_session:
        app.logger.debug('We do not yet have session data for the cli client')
        return jsonify(response), 202

    if not verify_cli_token(cli_token, session=local_session):
        return render_template('denied.html', reason='cli token verification failure'), 403

    if (local_session.get('sent_ap_session')):
        app.logger.error('Same cli client tried to get the ap_session data more than once (security risk)'
                         ', destroying session')
        session.clear()
        return render_template('denied.html', reason='Session was already issued'), 403

    response['ap_session'] = local_session.get('ap_session')
    response['cli_token_authenticated'] = True
    session['sent_ap_session'] = True
    app.logger.debug('Delivering session data to cli client')
    return jsonify(response), 200

@app.route('/api/ping')
def api_ping():
    return jsonify({'PONG': time.time()}), 200

@app.route('/api/ssh/certificate')
def api_ssh_certificate():
    """
    Returns the public key of the access proxy certificate
    """

    SSH_CA_PUB = './scripts/ca_user_key.pub'
    verify_user_ca()

    with open(SSH_CA_PUB, 'rb') as fd:
        response = {}
        response['certificate'] = fd.read()

    if response:
        return jsonify(response), 200
    else:
        return render_template('denied.html', reason='No SSH Public CA'), 500

@app.route('/api/ssh/', methods=['GET'])
def api_ssh():
    """
    Requests a new, valid SSH certificate
    """

    SSH_GEN_SCRIPT = './scripts/04_gen_signed_client_key.sh'
    SSH_FILES_DIR = '/dev/shm/ssh/'
    SSH_KEY_FILE = SSH_FILES_DIR + 'key_file'

    verify_user_ca()

    response = {'private_key': '', 'public_key': '', 'certificate': ''}

    # GET parameter set up
    cli_token = request.args.get('cli_token')

    local_session = load_session_hack(cli_token)
    if not verify_cli_token(cli_token, session=local_session):
        return 'Access denied', 403

    # Get user supplied username, if not available use profile user name
    username = local_session.get('ssh_user')
    if not username:
        username = local_session.get('username')
        username = username.replace('@', '_')
    ecode = subprocess.call([SSH_GEN_SCRIPT, username])
    app.logger.debug('Ran SSH_GEN_SCRIPT exit code is {}'.format(ecode))
    if ecode != 0:
        return render_template('denied.html', reason='SSH credentials generation failed'), 500
    try:
        with open(SSH_KEY_FILE, 'rb') as fd:
            response['private_key'] = fd.read()
        os.remove(SSH_KEY_FILE)
        with open(SSH_KEY_FILE + '.pub', 'rb') as fd:
            response['public_key'] = fd.read()
        os.remove(SSH_KEY_FILE + '.pub')
        with open(SSH_KEY_FILE + '-cert.pub', 'rb') as fd:
            response['certificate'] = fd.read()
        os.remove(SSH_KEY_FILE + '-cert.pub')
    except:
        import traceback
        app.logger.debug(traceback.format_exc())
        app.logger.debug('Failed to read SSH credentials')
        return render_template('denied.html', reason='SSH credentials generation failed'), 500

    app.logger.debug('Deliverying SSH key data to cli client')
    return jsonify(response), 200
