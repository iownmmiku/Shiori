"""Bounded SQL aggregates and process metrics; no synthetic performance data."""
import asyncio
import ctypes
import os
from pathlib import Path
import threading
import time
from datetime import datetime, timedelta, timezone

from .timezones import config_zone, day_bounds
from .resource_metrics import ResourceMetrics


class ProcessMetrics:
    def __init__(self):
        self.started = time.monotonic()
        self.sample = None
        self.lock = threading.Lock()

    def read(self):
        now = time.monotonic()
        cpu = sum(os.times()[:2])
        with self.lock:
            previous = self.sample
            self.sample = (now, cpu)
        percent = None if previous is None else max(0, min(100, (cpu-previous[1])/max(.001,now-previous[0])*100/max(1,os.cpu_count() or 1)))
        memory = None
        try:
            if os.name == 'nt':
                class Counters(ctypes.Structure):
                    _fields_=[('cb',ctypes.c_ulong),('faults',ctypes.c_ulong)]+[(key,ctypes.c_size_t) for key in
                        ('peak_rss','rss','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile')]
                counters=Counters();counters.cb=ctypes.sizeof(counters)
                process=ctypes.windll.kernel32.GetCurrentProcess
                process.restype=ctypes.c_void_p
                if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(process()),ctypes.byref(counters),counters.cb):memory=int(counters.rss)
            elif Path('/proc/self/statm').exists():
                memory=int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
        except (OSError,ValueError,AttributeError,IndexError):pass
        return {'cpu_percent':percent,'cpu_basis':'share_of_host_cpus','memory_bytes':memory,
                'uptime_seconds':max(0,now-self.started)}


def snapshot(store,settings,days=1,*,now=None):
    if type(days) is not int or days not in (1,3,7):raise ValueError('统计范围仅支持1、3或7天')
    now=now or datetime.now(timezone.utc)
    start=now-timedelta(days=days)
    stamp=lambda value:value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    params=(stamp(start),stamp(now))
    day_start,day_end=day_bounds(settings,now=now)
    platform="CASE WHEN scope_id LIKE 'qqofficial:%' THEN 'qqofficial' WHEN scope_id LIKE 'qq:%' THEN 'onebot' WHEN scope_id LIKE 'web:%' THEN 'web' ELSE 'other' END"
    with store._lock:
        connection=store._conn
        messages_total=connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        messages=connection.execute("SELECT CAST(strftime('%s',created_at) AS INTEGER)/3600,COUNT(*) FROM messages WHERE created_at>=? AND created_at<? GROUP BY 1",params).fetchall()
        platforms=connection.execute('SELECT '+platform+',COUNT(*) FROM messages WHERE created_at>=? AND created_at<? GROUP BY 1 ORDER BY 2 DESC',params).fetchall()
        usage=connection.execute("SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),COALESCE(SUM(cost),0),COALESCE(SUM(CASE WHEN json_extract(data,'$.cost_known')=0 OR (json_extract(data,'$.cost_known') IS NULL AND cost=0) THEN 1 ELSE 0 END),0),COALESCE(SUM(CASE WHEN json_extract(data,'$.estimated')=1 THEN 1 ELSE 0 END),0),AVG(CASE WHEN COALESCE(json_extract(data,'$.unconfirmed'),0)=0 AND json_type(data,'$.latency_ms') IN ('integer','real') THEN json_extract(data,'$.latency_ms') END) FROM usage WHERE created_at>=? AND created_at<?",params).fetchone()
        model_rows=connection.execute("SELECT COALESCE(json_extract(data,'$.provider_id'),'default'),COALESCE(json_extract(data,'$.model'),'未知模型'),COUNT(*),SUM(input_tokens+output_tokens) FROM usage WHERE created_at>=? AND created_at<? GROUP BY 1,2 ORDER BY 4 DESC LIMIT 100",params).fetchall()
        usage_hours=connection.execute("SELECT CAST(strftime('%s',created_at) AS INTEGER)/3600,SUM(input_tokens+output_tokens) FROM usage WHERE created_at>=? AND created_at<? GROUP BY 1",params).fetchall()
        today=connection.execute('SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE created_at>=? AND created_at<?',(day_start,day_end)).fetchone()[0]
        ttft=connection.execute("SELECT AVG(json_extract(data,'$.ttft_ms')) FROM usage WHERE created_at>=? AND created_at<? AND COALESCE(json_extract(data,'$.unconfirmed'),0)=0 AND json_type(data,'$.ttft_ms') IN ('integer','real')",params).fetchone()[0]
    counts={int(row[0]):int(row[1]) for row in messages if row[0] is not None}
    tokens={int(row[0]):int(row[1]) for row in usage_hours if row[0] is not None}
    zone=config_zone(settings)
    from .usage_ledger import breakdown
    accounting = breakdown(store, params[0], params[1])
    first=int(start.timestamp())//3600;last=int(now.timestamp())//3600
    trend=[{'at':datetime.fromtimestamp(hour*3600,timezone.utc).isoformat(),
            'label':datetime.fromtimestamp(hour*3600,zone).strftime('%m-%d %H:%M'),
            'messages':counts.get(hour,0),'tokens':tokens.get(hour,0)} for hour in range(first,last+1)]
    return {'days':days,'timezone':str(zone),'start':params[0],'end':params[1],
            'messages_total':int(messages_total),'period_messages':sum(counts.values()),'tokens_today':int(today),
            'platforms':[{'platform':row[0],'messages':int(row[1])} for row in platforms],
            'models':[{'provider_id':row[0],'model':row[1],'calls':int(row[2]),'tokens':int(row[3] or 0)} for row in model_rows],
            'usage':{'calls':int(usage[0]),'input_tokens':int(usage[1]),'output_tokens':int(usage[2]),'tokens':int(usage[1]+usage[2]),
                     'estimated_cost':float(usage[3]),'unknown_price_calls':int(usage[4]),'estimated_calls':int(usage[5]),
                     'accounting':accounting,
                     'average_response_ms':None if usage[6] is None else float(usage[6]),'ttft_ms':None if ttft is None else float(ttft)},'trend':trend}


def install_statistics(app,store,settings,onebot,official):
    metrics=ProcessMetrics()
    resources=ResourceMetrics()
    @app.get('/api/statistics')
    async def statistics(days:int=1):
        result=await asyncio.to_thread(snapshot,store,settings,days)
        result['process']=metrics.read()
        result['resources']=await asyncio.to_thread(resources.read)
        result['connector_resources']={'available':False,'reason':'external_process_identity_unknown',
            'message':'NapCat / SnowLuma 由外部运行，尚无可信进程标识，其独立资源占用未单独采集。'}
        memory=getattr(getattr(app.state,'chat',None),'memory',None)
        if callable(status:=getattr(memory,'indexing_status',None)):
            result['memory_indexing']=status()
        result['admission']=app.state.resource_limits()
        result['connections']=[{'platform':'web','connected':True},
            {'platform':'onebot','connected':bool(onebot.status().get('connected'))},
            {'platform':'qqofficial','connected':bool(official.status().get('connected'))}]
        result['online_platforms']=sum(item['connected'] for item in result['connections'])
        return result
