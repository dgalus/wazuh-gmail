#!/usr/bin/python3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import asyncore
import base64
import email.message
import imaplib
import json
import lxml.html
import optparse
import os
import smtpd
import smtplib
import ssl
import sys
import threading
import urllib.parse
import urllib.request


CONFIG_FILE_PATH = "wazuh-gmail.conf"
GOOGLE_ACCOUNTS_BASE_URL = 'https://accounts.google.com'
REDIRECT_URI = 'https://oauth2.dance/'


def SetupOptionParser():
  parser = optparse.OptionParser(usage=__doc__)
  parser.add_option('--configure',
                    action='store_true',
                    dest='configure',
                    help='Configure SMTP relay for Gmail')
  parser.add_option('--run',
                    action='store_true',
                    dest='run_server',
                    help='Run SMTP relay for Gmail')
  return parser


def AccountsUrl(command):
  """Generates the Google Accounts URL.

  Args:
    command: The command to execute.

  Returns:
    A URL for the given command.
  """
  return '%s/%s' % (GOOGLE_ACCOUNTS_BASE_URL, command)


def UrlEscape(text):
  # See OAUTH 5.1 for a definition of which characters need to be escaped.
  return urllib.parse.quote(text, safe='~-._')


def CommandToUrl(command):
  return '%s/%s' % (GOOGLE_ACCOUNTS_BASE_URL, command)


def FormatUrlParams(params):
  """Formats parameters into a URL query string.

  Args:
    params: A key-value map.

  Returns:
    A URL query string version of the given parameters.
  """
  param_fragments = []
  for param in sorted(params.items(), key=lambda x: x[0]):
    param_fragments.append('%s=%s' % (param[0], UrlEscape(param[1])))
  return '&'.join(param_fragments)


def GeneratePermissionUrl(client_id, scope='https://mail.google.com/'):
  """Generates the URL for authorizing access.

  This uses the "OAuth2 for Installed Applications" flow described at
  https://developers.google.com/accounts/docs/OAuth2InstalledApp

  Args:
    client_id: Client ID obtained by registering your app.
    scope: scope for access token, e.g. 'https://mail.google.com'
  Returns:
    A URL that the user should visit in their browser.
  """
  params = {}
  params['client_id'] = client_id
  params['redirect_uri'] = REDIRECT_URI
  params['scope'] = scope
  params['response_type'] = 'code'
  params['access_type'] = 'offline'
  params['prompt'] = 'consent'
  return '%s?%s' % (AccountsUrl('o/oauth2/auth'),
                    FormatUrlParams(params))


def AuthorizeTokens(client_id, client_secret, authorization_code):
  """Obtains OAuth access token and refresh token.

  This uses the application portion of the "OAuth2 for Installed Applications"
  flow at https://developers.google.com/accounts/docs/OAuth2InstalledApp#handlingtheresponse

  Args:
    client_id: Client ID obtained by registering your app.
    client_secret: Client secret obtained by registering your app.
    authorization_code: code generated by Google Accounts after user grants
        permission.
  Returns:
    The decoded response from the Google Accounts server, as a dict. Expected
    fields include 'access_token', 'expires_in', and 'refresh_token'.
  """
  params = {}
  params['client_id'] = client_id
  params['client_secret'] = client_secret
  params['code'] = authorization_code
  params['redirect_uri'] = REDIRECT_URI
  params['grant_type'] = 'authorization_code'
  request_url = AccountsUrl('o/oauth2/token')
  response = urllib.request.urlopen(request_url, urllib.parse.urlencode(params).encode('utf-8')).read()
  return json.loads(response)


def CallRefreshToken(client_id, client_secret, refresh_token):
  params = {}
  params['client_id'] = client_id
  params['client_secret'] = client_secret
  params['refresh_token'] = refresh_token
  params['grant_type'] = 'refresh_token'
  request_url = CommandToUrl('o/oauth2/token')
  response = urllib.request.urlopen(request_url, urllib.parse.urlencode(params).encode('UTF-8')).read().decode('UTF-8')
  return json.loads(response)


def RequireOptions(options, *args):
  missing = [arg for arg in args if getattr(options, arg) is None]
  if missing:
    print('Missing options: %s' % ' '.join(missing))
    sys.exit(-1)


def GenerateOauth2String(username, access_token, as_base64=False):
  auth_string = 'user=%s\1auth=Bearer %s\1\1' % (username, access_token)
  if as_base64:
    auth_string = base64.b64encode(auth_string.encode('ascii')).decode('ascii')
  return auth_string


def RefreshAuthorization(google_client_id, google_client_secret, refresh_token):
  response = CallRefreshToken(google_client_id, google_client_secret, refresh_token)
  return response['access_token'], response['expires_in']


def SendMail(fromaddr, toaddr, subject, message, gmail_config, is_multipart=True):
  access_token, expires_in = RefreshAuthorization(gmail_config['client_id'], gmail_config['client_secret'], gmail_config['refresh_token'])
  auth_string = GenerateOauth2String(fromaddr, access_token, as_base64=True)
  msg = email.message.Message()
  msg['Subject'] = subject
  msg['From'] = fromaddr
  msg['To'] = toaddr
  msg.add_header('Content-Type', 'text')
  msg.set_payload(message)
  server = smtplib.SMTP('smtp.gmail.com:587')
  server.ehlo(gmail_config['client_id'])
  server.starttls()
  server.docmd('AUTH', 'XOAUTH2 ' + auth_string)
  server.sendmail(fromaddr, toaddr, bytes(msg.as_string(), 'utf-8'))
  server.quit()


def ToHtml(txt):
  return txt.replace(b"\n", b"<br>").replace(b"\r", b"<br>").replace(b"\t", b"&nbsp;")


class SMTPServer():
  def start(self):
    '''Start listening'''
    self.smtp = CustomSMTPServer(('127.0.0.1', 25), None)
    kwargs = {'timeout':1, 'use_poll': True}
    self.thread = threading.Thread(target=asyncore.loop, kwargs=kwargs)
    self.thread.start()

  def stop(self):
    '''Stop listening'''
    self.smtp.close()
    self.thread.join()

  def get(self):
    '''Return all emails received so far'''
    return self.smtp.emails


class CustomSMTPServer(smtpd.SMTPServer):
  def set_gmail_config(self, config):
    self.config = config

  def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
    subject = data.decode('utf-8').split("Subject: ")[1].split("\n")[0]
    messages = data.decode('utf-8').split('END OF NOTIFICATION')
    for r in rcpttos:
      for msg in messages:
        if len(msg) > 10:
          SendMail(mailfrom, r, subject, data, self.config, False)
    return


def main(argv):
  options_parser = SetupOptionParser()
  (options, args) = options_parser.parse_args()

  if options.configure:
    client_id = input('Enter Client ID: ')
    client_secret = input('Enter Client Secret: ')
    user = input('Enter gmail mail address: ')
    permission_url = GeneratePermissionUrl(options.client_id)
    print("Please open this URL in your browser and follow the instructions: {}".format(permission_url))
    authorization_code = input('Enter verification code: ')
    response = AuthorizeTokens(options.client_id, options.client_secret, authorization_code)

    cfg = {}
    cfg['user'] = user
    cfg['client_id'] = client_id
    cfg['client_secret'] = client_secret
    cfg['refresh_token'] = response['refresh_token']

    with open(CONFIG_FILE_PATH, 'w') as c:
      c.write(json.dumps(cfg))
    print("Configuration file saved.")
  elif options.run_server:
    if not os.path.isfile(CONFIG_FILE_PATH):
      print("Config file not detected. Please run '{} --configure' first.".format(sys.argv[0]))
      return
    with open(CONFIG_FILE_PATH) as c:
      cfg_str = c.read()
    cfg = json.loads(cfg_str)
    server = CustomSMTPServer(('127.0.0.1', 25), None)
    server.set_gmail_config(cfg)
    asyncore.loop()
  else:
    options_parser.print_help()
    print('Nothing to do, exiting.')
    return


if __name__ == '__main__':
  main(sys.argv)
