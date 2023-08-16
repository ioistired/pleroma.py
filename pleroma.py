# SPDX-License-Identifier: AGPL-3.0-only

import sys
import yarl
import json
import anyio
import hashlib
import aiohttp
import contextlib
from http import HTTPStatus
from pathlib import Path, PurePath
from datetime import datetime, timezone
from dateutil.parser import parse as parsedate

__version__ = '0.0.4'

class BadRequest(Exception):
	pass

class LoginFailed(Exception):
	pass

class BadResponse(Exception):
	pass

async def sleep_until(dt):
	await anyio.sleep((dt - datetime.now(timezone.utc)).total_seconds())

class HandleRateLimits:
	def __init__(self, http):
		self.http = http

	def request(self, *args, **kwargs):
		return _RateLimitContextManager(self.http, args, kwargs)

class _RateLimitContextManager(contextlib.AbstractAsyncContextManager):
	def __init__(self, http, args, kwargs):
		self.http = http
		self.args = args
		self.kwargs = kwargs

	async def __aenter__(self):
		self._request_cm = self.http.request(*self.args, **self.kwargs)
		return await self._do_enter()

	async def _do_enter(self):
		resp = await self._request_cm.__aenter__()
		if resp.headers.get('X-RateLimit-Remaining') not in {'0', '1'}:
			return resp

		await sleep_until(parsedate(resp.headers['X-RateLimit-Reset']))
		await self._request_cm.__aexit__(*(None,)*3)
		return await self.__aenter__()

	async def __aexit__(self, *excinfo):
		return await self._request_cm.__aexit__(*excinfo)

def _http_session_factory(headers={}, **kwargs):
	py_version = '.'.join(map(str, sys.version_info))
	user_agent = (
		'pleroma.py (https://github.com/ioistired/pleroma.py); '
		'aiohttp/{aiohttp.__version__}; '
		'python/{py_version}'
	)
	return aiohttp.ClientSession(
		headers={'User-Agent': user_agent, **headers},
		**kwargs,
	)

class File:
	def __init__(
		self,
		fp,
		filename=None,
		mime_type=None,
		*,
		description=None,
		focus=None,
		thumbnail=None,
		thumbnail_mime_type=None,
	):
		if filename is None and isinstance(fp, str):
			self.filename = PurePath(fp).name
		try:
			self.fp = open(fp, 'rb')
		except TypeError:
			# probably already a file-like
			self.fp = fp
			self.filename = PurePath(fp.name).name
		else:
			self.filename = PurePath(self.fp.name).name

		self.mime_type = mime_type
		self.description = description
		self.focus = focus

		if thumbnail is not None:
			self.thumbnail = type(self)(thumbnail, mime_type=thumbnail_mime_type)
		else:
			self.thumbnail = thumbnail

class Pleroma:
	def __init__(self, *, api_base_url, access_token):
		self.api_base_url = api_base_url.rstrip('/')
		self.access_token = access_token.strip()
		self._session = _http_session_factory({'Authorization': 'Bearer ' + self.access_token})
		self._rl_handler = HandleRateLimits(self._session)
		self._logged_in_id = None

	async def __aenter__(self):
		self._session = await self._session.__aenter__()
		return self

	async def __aexit__(self, *excinfo):
		return await self._session.__aexit__(*excinfo)

	async def request(self, method, path, **kwargs):
		# blocklist of some horrible instances
		if hashlib.sha256(
			yarl.URL(self.api_base_url).host.encode()
			+ bytes.fromhex('d590e3c48d599db6776e89dfc8ebaf53c8cd84866a76305049d8d8c5d4126ce1')
		).hexdigest() in {
			'56704d4d95b882e81c8e7765e9079be0afc4e353925ba9add8fd65976f52db83',
			'1932431fa41a0baaccce7815115b01e40e0237035bb155713712075b887f5a19',
			'a42191105a9f3514a1d5131969c07a95e06d0fdf0058f18e478823bf299881c9',
		}:
			raise RuntimeError('stop being a chud')

		async with self._rl_handler.request(method, self.api_base_url + path, **kwargs) as resp:
			if resp.status == HTTPStatus.BAD_REQUEST:
				raise BadRequest((await resp.json())['error'])
			if resp.status == HTTPStatus.INTERNAL_SERVER_ERROR:
			    raise BadResponse(await resp.json())
			#resp.raise_for_status()
			return await resp.json()

	async def verify_credentials(self):
		return await self.request('GET', '/api/v1/accounts/verify_credentials')

	me = verify_credentials

	async def _get_logged_in_id(self):
		if self._logged_in_id is not None:
			return self._logged_in_id

		me = await self.me()

		try:
			self._logged_in_id = me['id']
		except KeyError:
			raise LoginFailed(me)

		return self._logged_in_id

	async def following(self, account_id=None):
		account_id = account_id or await self._get_logged_in_id()
		return await self.request('GET', f'/api/v1/accounts/{account_id}/following')

	@staticmethod
	def _unpack_id(obj):
		if isinstance(obj, dict) and 'id' in obj:
			return obj['id']
		return obj

	async def status_context(self, id):
		id = self._unpack_id(id)
		return await self.request('GET', f'/api/v1/statuses/{id}/context')

	async def media(self, id):
		id = self._unpack_id(id)
		return await self.request('GET', f'/api/v1/media/{id}')

	async def upload(self, file):
		data = aiohttp.FormData()
		data.add_field('file', file.fp, filename=file.filename, content_type=file.mime_type)
		focus = None
		if file.focus is not None:
			if len(file.focus) != 2:
				raise ValueError('focus must be a sequence of 2 floats')
			focus = ','.join(file.focus)
		if file.thumbnail is not None:
			data.add_field(
				'thumbnail',
				file.thumbnail.fp,
				filename=file.thumbnail.filename,
				content_type=file.thumbnail.mime_type,
			)

		params = {}
		if focus:
			params['focus'] = focus
		if file.description is not None:
			params['description'] = file.description

		return await self.request(
			'POST', '/api/v1/media',
			data=data,
			params=params,
		)

	async def post(self, content, *, in_reply_to_id=None, cw=None, visibility=None, files=None):
		if visibility not in {None, 'private', 'public', 'unlisted', 'direct'}:
			raise ValueError('invalid visibility', visibility)

		data = dict(status=content)
		if in_reply_to_id := self._unpack_id(in_reply_to_id):
			data['in_reply_to_id'] = in_reply_to_id
		if visibility is not None:
			data['visibility'] = visibility
		# normally, this would be a check against None.
		# however, apparently Pleroma serializes posts without CWs as posts with an empty string
		# as a CW, so per the robustness principle we'll accept that too.
		if cw:
			data['spoiler_text'] = cw

		if files:
			files_uploaded = [None] * len(files)
			async with anyio.create_task_group() as tg:
				async def upload(i, file):
					files_uploaded[i] = (await self.upload(file))['id']

				for i, file in enumerate(files):
					tg.start_soon(upload, i, file)

			assert None not in files_uploaded
			data['media_ids[]'] = files_uploaded

		return await self.request('POST', '/api/v1/statuses', data=data)

	async def reply(self, to_status, content, *, cw=None, files=None):
		user_id = await self._get_logged_in_id()

		mentioned_accounts = {}
		mentioned_accounts[to_status['account']['id']] = to_status['account']['acct']
		for account in to_status['mentions']:
			if account['id'] != user_id and account['id'] not in mentioned_accounts:
				mentioned_accounts[account['id']] = account['acct']

		content = ''.join('@' + x + ' ' for x in mentioned_accounts.values()) + content

		visibility = 'unlisted' if to_status['visibility'] == 'public' else to_status['visibility']
		if not cw and 'spoiler_text' in to_status and to_status['spoiler_text']:
			cw = 're: ' + to_status['spoiler_text']

		return await self.post(
			content,
			in_reply_to_id=to_status['id'],
			cw=cw,
			visibility=visibility,
			files=files,
		)

	async def delete_status(self, id):
		id = self._unpack_id(id)
		return await self.request('DELETE', f'/api/v1/statuses/{id}')

	async def favorite(self, id):
		id = self._unpack_id(id)
		return await self.request('POST', f'/api/v1/statuses/{id}/favourite')

	async def unfavorite(self, id):
		id = self._unpack_id(id)
		return await self.request('POST', f'/api/v1/statuses/{id}/unfavourite')

	async def repeat(self, id):
		id = self._unpack_id(id)
		return await self.request('POST', f'/api/v1/statuses/{id}/reblog')

	async def un_repeat(self, id):
		id = self._unpack_id(id)
		# why this is a POST and not a DELETE is beyond me...
		return await self.request('POST', f'/api/v1/statuses/{id}/unreblog')

	async def react(self, id, reaction):
		id = self._unpack_id(id)
		return await self.request('PUT', f'/api/v1/pleroma/statuses/{id}/reactions/{reaction}')

	async def remove_reaction(self, id, reaction):
		id = self._unpack_id(id)
		return await self.request('DELETE', f'/api/v1/pleroma/statuses/{id}/reactions/{reaction}')

	async def pin(self, id):
		id = self._unpack_id(id)
		return await self.request('POST', f'/api/v1/statuses/{id}/pin')

	async def unpin(self, id):
		id = self._unpack_id(id)
		return await self.request('POST', f'/api/v1/statuses/{id}/unpin')

	async def account_statuses(self, id, *, exclude_repeats=False, max_id=None, limit=None):
		id = self._unpack_id(id)
		params = dict(exclude_reblogs='1' if exclude_repeats else '0')
		if max_id is not None: params['max_id'] = self._unpack_id(max_id)
		return await self.request('GET', f'/api/v1/accounts/{id}/statuses', params=params)

	async def account_statuses_iter(self, id, *, exclude_repeats=False, max_id=None):
		while results := await self.account_statuses(
			id,
			exclude_repeats=exclude_repeats,
			max_id=max_id,
		):
			for result in results:
				yield result
			max_id = results[-1]

	async def stream(self, stream_name, *, target_event_type=None):
		async with self._session.ws_connect(
			self.api_base_url + f'/api/v1/streaming?stream={stream_name}&access_token={self.access_token}'
		) as ws:
			async for msg in ws:
				if msg.type == aiohttp.WSMsgType.TEXT:
					event = msg.json()
					# the only event type that doesn't define `payload` is `filters_changed`
					if event['event'] == 'filters_changed':
						yield event
					elif target_event_type is None or event['event'] == target_event_type:
						# don't ask me why the payload is also JSON encoded smh
						yield json.loads(event['payload'])

	async def stream_notifications(self):
		async for notif in self.stream('user:notification', target_event_type='notification'):
			yield notif

	async def stream_mentions(self):
		async for notif in self.stream_notifications():
			if notif['type'] == 'mention':
				yield notif

	async def stream_local_timeline(self):
		async for notif in self.stream('public:local'):
			yield notif

	async def stream_federated_timeline(self):
		async for notif in self.stream('public'):
			yield notif
