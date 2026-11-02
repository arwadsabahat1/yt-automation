import json
from datetime import datetime, timedelta, timezone

import pytest

from core.db import connect
from income.clients import assess, draft, export, find, save_lead, schema, seeds, set_status
from research.youtube import YouTubeClient, QuotaExceeded

NOW=datetime(2026,10,31,tzinfo=timezone.utc)


def snapshot():
    return {'available':True,'channel_id':'UC_example','name':'Example Podcast','subs':5000,
            'listed_count':6,'more_uploads':True,'videos':[
                {'video_id':f'video{i}','channel_id':'UC_example','title':f'Founder interview {i}',
                 'published_at':(NOW-timedelta(days=i+1)).isoformat(),'duration_s':1200,'views':1500,'language':'en'}
                for i in range(6)]}


def test_candidate_evidence_distinguishes_sample_from_whole_channel():
    s=snapshot();s['videos'][0]['duration_s']=90
    lead=assess(s,NOW)
    assert lead['short_length_count']==1 and lead['recent_topic_long_count']==5
    assert lead['more_uploads'] is True
    assert any('not proof' in x for x in lead['limits'])
    assert 'no Shorts' not in draft(lead) and 'I watched' not in draft(lead)
    assert lead['example']['title'] in draft(lead)


@pytest.mark.parametrize('change', ['old','future','small_sample','large_channel','many_short','wrong_channel','foreign_language','unknown_duration'])
def test_unqualified_or_incomplete_samples_are_not_leads(change):
    s=snapshot()
    if change=='old':
        for v in s['videos']:v['published_at']=(NOW-timedelta(days=60)).isoformat()
    elif change=='future':
        for v in s['videos']:v['published_at']=(NOW+timedelta(days=1)).isoformat()
    elif change=='small_sample':s['videos']=s['videos'][:4]
    elif change=='large_channel':s['subs']=1000000
    elif change=='many_short':
        for v in s['videos'][:3]:v['duration_s']=90
    elif change=='wrong_channel':
        for v in s['videos']:v['channel_id']='other'
    elif change=='foreign_language':
        for v in s['videos']:v['language']='hi'
    elif change=='unknown_duration':
        for v in s['videos']:v['duration_s']=0
    assert assess(s,NOW) is None


def test_refresh_preserves_manual_status_note_and_draft(tmp_path):
    conn=connect(tmp_path/'db.sqlite');lead=assess(snapshot(),NOW)
    save_lead(conn,lead);set_status(conn,'UC_example','contacted','Owner sent it manually')
    export(conn,[lead],[],1,tmp_path/'out')
    path=tmp_path/'out/leads/UC_example/draft.txt';path.write_text('My edited draft')
    lead['name']='Updated name';save_lead(conn,lead);export(conn,[lead],[],1,tmp_path/'out')
    row=conn.execute('SELECT * FROM client_leads').fetchone()
    assert row['status']=='contacted' and row['note']=='Owner sent it manually'
    assert row['name']=='Updated name' and path.read_text()=='My edited draft'
    conn.close()


def test_output_escapes_untrusted_titles_and_csv_formulas(tmp_path):
    conn=connect(tmp_path/'db.sqlite');lead=assess(snapshot(),NOW)
    lead['name']='=HYPERLINK("bad")<script>alert(1)</script>'
    save_lead(conn,lead);export(conn,[lead],[],1,tmp_path/'out')
    assert '<script>' not in (tmp_path/'out/index.html').read_text()
    assert "'=HYPERLINK" in (tmp_path/'out/leads.csv').read_text(encoding='utf-8-sig')
    conn.close()


def test_status_validation_and_missing_lead(tmp_path):
    conn=connect(tmp_path/'db.sqlite')
    with pytest.raises(ValueError):set_status(conn,'missing','sent-automatically')
    with pytest.raises(ValueError):set_status(conn,'missing','contacted')
    conn.close()


def test_public_upload_sampling_uses_playlist_and_handles_unavailable_ids(monkeypatch):
    yt=YouTubeClient('unused',None,100);calls=[]
    def request(endpoint,**params):
        calls.append((endpoint,params))
        if endpoint=='channels':return {'items':[{'snippet':{'title':'Podcast'},'statistics':{'hiddenSubscriberCount':True},'contentDetails':{'relatedPlaylists':{'uploads':'uploads_id'}}}]}
        return {'items':[{'contentDetails':{'videoId':'visible'}},{'contentDetails':{'videoId':'unavailable'}}],'nextPageToken':'more'}
    monkeypatch.setattr(yt,'_get',request)
    monkeypatch.setattr(yt,'videos',lambda ids: [{'video_id':ids[0]}])
    result=yt.recent_uploads('UC_example',12)
    assert [c[0] for c in calls]==['channels','playlistItems']
    assert calls[1][1]['maxResults']==12
    assert result['listed_count']==2 and len(result['videos'])==1
    assert result['subs'] is None and result['more_uploads'] is True


def test_quota_failure_stops_without_leaking_request_secrets(tmp_path,monkeypatch):
    conn=connect(tmp_path/'db.sqlite');schema(conn)
    monkeypatch.setattr('income.clients.seeds',lambda *args:['one','two'])
    class API:
        calls=0
        def recent_uploads(self,*args):
            self.calls+=1;raise QuotaExceeded('https://api.example?key=SECRET')
    api=API();root,leads,errors=find(conn,api,1,2,tmp_path/'out')
    assert api.calls==1 and not leads and errors
    assert 'SECRET' not in (root/'report.json').read_text()
    conn.close()
