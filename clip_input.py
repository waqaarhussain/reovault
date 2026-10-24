"""UK-style date and flexible 12/24-hour time parsing for the local download helper."""
import re
from datetime import datetime,time,timedelta,timezone
from zoneinfo import ZoneInfo


def date_input(value,zone,now=None):
    now=now or datetime.now(zone)
    value=value.strip().lower()
    if value in ('','today'):return now.date()
    if value=='yesterday':return now.date()-timedelta(days=1)
    for fmt in ('%d/%m','%d-%m','%d%m'):
        try:return datetime.strptime(value+('/'+str(now.year) if fmt=='%d/%m' else '-'+str(now.year) if fmt=='%d-%m' else str(now.year)),fmt+('/%Y' if fmt=='%d/%m' else '-%Y' if fmt=='%d-%m' else '%Y')).date()
        except ValueError:pass
    raise ValueError('Enter DD/MM without a year, for example 24/10, or today/yesterday.')


def time_input(value):
    value=re.sub(r'\s+','',value.lower())
    if value in ('noon','midday'):return time(12)
    if value=='midnight':return time(0)
    for fmt in ('%I:%M%p','%I%M%p','%I%p','%H:%M','%H%M'):
        try:return datetime.strptime(value,fmt).time()
        except ValueError:pass
    raise ValueError('Use a time such as 0757pm, 08:10pm, 19:57 or 2010.')


def range_input(value):
    parts=re.split(r'\s*[-–]\s*',value.strip())
    if len(parts)!=2:raise ValueError('Enter start - end, for example 0757pm - 08:10pm.')
    return time_input(parts[0]),time_input(parts[1])


def local_time(day,clock,zone,choose=None):
    naive=datetime.combine(day,clock)
    candidates=[]
    for fold in (0,1):
        value=naive.replace(tzinfo=zone,fold=fold)
        if value.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)==naive and all(x.utcoffset()!=value.utcoffset() for x in candidates):candidates.append(value)
    if not candidates:raise ValueError('That local time does not exist because the clocks move forward. Choose another time.')
    if len(candidates)>1:
        if choose is None:raise ValueError('That time occurs twice when the clocks go back. Choose first or second occurrence.')
        index=int(choose(naive,candidates))-1
        if index not in (0,1):raise ValueError('Choose 1 or 2.')
        return candidates[index]
    return candidates[0]


def interval(day,first,last,zone,choose=None):
    start=local_time(day,first,zone,choose)
    end_day=day+timedelta(days=1) if last<first else day
    end=local_time(end_day,last,zone,choose)
    if end.timestamp()<=start.timestamp():raise ValueError('End time must be later than start time.')
    return start,end
