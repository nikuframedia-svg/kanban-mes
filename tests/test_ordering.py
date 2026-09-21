from app import db

def test_capture_order_with_stable_ties_before_filtering(tmp_path):
    with db.connect(tmp_path/'db.sqlite') as conn:
        ids=[]
        for date in ('2026-09-20 10:00:00','2026-09-21 10:00:00','2026-09-21 10:00:00','2026-09-19 10:00:00'):
            uid=db.create_sheet(conn, 'cantoneiras_kanban' if 'mtg2' not in __file__ else 'tpl999_kanban')
            ids.append(uid)
            db.set_extraction(conn,uid,{'header':{'operador':'ORDER'},'rows':[]})
            conn.execute('UPDATE sheets SET created_at=? WHERE uid=?',(date,uid));conn.commit()
        for filters in ({},{'status':'pending'},{'operador':'ORDER'}):
            assert [r['uid'] for r in db.list_sheets(conn,**filters)] == [ids[2],ids[1],ids[0],ids[3]]
        assert [db.get_sheet(conn,uid)['sheet_no'] for uid in ids] == [1,2,3,4]
