"""Local HTTP integration with invented metadata and no cloud calls."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from dashboard import app


class LibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.contacts = self.data / 'contacts'
        bundle = self.contacts / 'synthetic__one'
        bundle.mkdir(parents=True)
        self.messages = bundle / 'messages.json'
        self.messages.write_text(json.dumps({'contact_display':'合成会话','messages':[]}), encoding='utf-8')
        self.original = self.messages.read_bytes()
        (bundle / 'dashboard_manifest.json').write_text(json.dumps({'contact':'合成会话','message_count':0}), encoding='utf-8')
        self.patches = [mock.patch.object(app,'DATA_DIR',self.data),
                        mock.patch.object(app,'CONTACTS_DIR',self.contacts),
                        mock.patch.object(app,'exporter_status',return_value={})]
        for patch in self.patches:
            patch.start()
        self.server = app.create_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        with urllib.request.urlopen(self.base) as response:
            self.cookie = response.headers['Set-Cookie'].split(';')[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def request(self,path,body=None,*,auth=True,origin=True):
        headers = {'Content-Type':'application/json'}
        if auth:
            headers['Cookie']=self.cookie
        if origin:
            headers['Origin']=self.base
        request = urllib.request.Request(self.base+path,headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            return error.code,json.load(error)

    def test_management_roundtrip_conflict_and_source_immutability(self):
        status, library = self.request('/api/library')
        self.assertEqual(status,200)
        self.assertEqual(len(library['conversations']),1)
        status, response = self.request('/api/library/tags',{'name':'待跟进','color':'#365ecb'})
        self.assertEqual(status,200)
        tag = response['tag']
        patch = {'bundle_id':'synthetic__one','expected_version':0,'alias':'展示别名',
                 'note':'本机备注','pinned':True,'tag_ids':[tag['id']]}
        self.assertEqual(self.request('/api/library/conversations',patch)[0],200)
        self.assertEqual(self.request('/api/library/conversations',patch)[0],409)
        item = self.request('/api/state')[1]['bundles'][0]
        self.assertEqual(item['contact'],'合成会话')
        self.assertEqual(item['library']['alias'],'展示别名')
        self.assertEqual(item['library']['tag_names'],['待跟进'])
        self.assertEqual(self.messages.read_bytes(),self.original)

    def test_trash_excluded_from_catalog_search_and_new_analysis_restorable(self):
        self.request('/api/library')
        patch = {'bundle_id':'synthetic__one','expected_version':0,'hidden':True}
        self.assertEqual(self.request('/api/library/conversations',patch)[0],200)
        self.assertEqual(self.request('/api/state')[1]['bundles'],[])
        fake = {'status':'ok','matches':[{'bundle_id':'synthetic__one','match_count':1}],
                'indexed_messages':1,'indexed_conversations':1}
        with mock.patch.object(app,'search_messages',return_value=fake):
            self.assertEqual(self.request('/api/search',{'query':'合成'})[1]['matches'],[])
        with mock.patch.object(app.scoped_ai,'preview') as preview:
            self.assertEqual(self.request('/api/ai/preview',{'bundle_id':'synthetic__one'})[0],409)
            preview.assert_not_called()
        patch.update(expected_version=1,hidden=False)
        self.assertEqual(self.request('/api/library/conversations',patch)[0],200)
        self.assertEqual(len(self.request('/api/state')[1]['bundles']),1)

    def test_module_disable_gates_backend_but_preserves_readonly_history(self):
        self.assertEqual(self.request('/api/modules',{'id':'analysis','enabled':False,'expected_version':0})[0],200)
        with mock.patch.object(app.scoped_ai,'run') as run:
            self.assertEqual(self.request('/api/ai/run',{'consent':True})[0],409)
            run.assert_not_called()
        self.assertEqual(self.request('/api/ai/history')[0],200)
        self.assertEqual(self.request('/api/modules',{'id':'backup','enabled':False,'expected_version':0})[0],200)
        with mock.patch.object(app,'start_backup') as backup:
            self.assertEqual(self.request('/api/backup/run',{})[0],409)
            backup.assert_not_called()
        self.assertEqual(self.request('/api/modules',{'id':'media','enabled':False,'expected_version':0})[0],409)

    def test_management_requires_auth_origin_and_rejects_arbitrary_mutations(self):
        self.assertEqual(self.request('/api/library',auth=False)[0],403)
        self.assertEqual(self.request('/api/modules',{'id':'sync','enabled':False,'expected_version':0},origin=False)[0],403)
        self.request('/api/library')
        self.assertEqual(self.request('/api/library/conversations',{'bundle_id':'synthetic__one','expected_version':0,'messages':[]})[0],400)
        self.assertEqual(self.request('/api/library/tags',{'name':'ok','color':'#365ecb','path':'elsewhere'})[0],400)

    @unittest.skipUnless(os.name=='nt','Windows sealed preview integration')
    def test_preview_hidden_before_run_is_not_consumed_or_billed(self):
        from datetime import datetime
        self.messages.write_text(json.dumps({'contact_display':'合成会话','my_display':'测试我','messages':[
            {'timestamp':datetime(2026,8,10,12).timestamp(),'sender':'them','type':'text','content':'仅用于测试的文字'}
        ]}),encoding='utf-8')
        self.request('/api/library')
        app.scoped_ai.save_config(self.data,{'provider':'openai','model':'synthetic-model','api_key':'synthetic-not-real-key'})
        with mock.patch.object(app.scoped_ai,'_cloud') as cloud:
            code, preview = self.request('/api/ai/preview',{'bundle_id':'synthetic__one',
                'date_from':'2026-08-01','date_to':'2026-08-31','focus':'communication','max_messages':100})
            self.assertEqual(code,200)
            path=self.data/'private/scoped-ai'/f"preview-{preview['preview_id']}.json"
            sealed=path.read_bytes()
            self.request('/api/library/conversations',{'bundle_id':'synthetic__one','expected_version':0,'hidden':True})
            self.assertEqual(self.request('/api/ai/run',{'preview_id':preview['preview_id'],'consent':True})[0],409)
            self.assertEqual(path.read_bytes(),sealed)
            self.assertEqual(list(path.parent.glob('job-*.json')),[])
            self.assertEqual(list(path.parent.glob('consumed-*.json')),[])
            cloud.assert_not_called()

    def test_disabled_media_cannot_be_reached_using_dot_segments(self):
        for folder in ('wechat-media','wechat-voice','classification'):
            target=self.data/'exports'/folder
            target.mkdir(parents=True)
            (target/'index.html').write_text('<p>synthetic gallery</p>')
        self.request('/api/modules',{'id':'voice','enabled':False,'expected_version':0})
        self.request('/api/modules',{'id':'media','enabled':False,'expected_version':0})
        for relative in ('wechat-media/index.html','classification/../wechat-media/index.html',
                         'classification/../wechat-voice/index.html',
                         'classification%5C..%5Cwechat-media%5Cindex.html'):
            with self.subTest(relative=relative):
                self.assertEqual(self.request('/exports/'+relative)[0],409 if os.name=='nt' or '%5C' not in relative else 404)


if __name__ == '__main__':
    unittest.main()
