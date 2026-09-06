import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from classify_wechat_local import classify_messages

class ClassificationTests(unittest.TestCase):
    def test_high_signal_business_labels_and_no_quotes(self):
        result=classify_messages([{'content':'本周商户入驻','type':'text'}]*8)
        self.assertEqual(result['primary_topic'],'商户与业务合作')
        self.assertNotIn('本周商户入驻',str(result))
    def test_weak_and_mixed_are_not_overclaimed(self):
        self.assertEqual(classify_messages([{'content':'商户'}])['primary_topic'],'待确认／低信号')
        self.assertEqual(classify_messages([{'content':'商户项目'}]*8)['primary_topic'],'混合主题')
    def test_voice_transcript_contributes_to_offline_classification(self):
        result = classify_messages([{'type':'voice','content':'[语音]', 'transcript':'商户入驻'}]*8)
        self.assertEqual(result['primary_topic'], '商户与业务合作')
        self.assertEqual(result['message_count'], 8)

if __name__=='__main__': unittest.main()
