"""Scoped user requirements. These guide answers, not certified ingredient filtering.
The parser accepts bounded explicit updates; unknown scope is clarified, never guessed.
"""
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Change:
    action: str
    scope: str
    kind: str = ''
    value: str = ''
    source: str = ''


@dataclass
class Parsed:
    changes: list = field(default_factory=list)
    only_update: bool = False
    ambiguous: bool = False


def parse_updates(query, current_name=None):
    result=Parsed()
    clauses=[x.strip() for x in re.split(r'[，,。；;！？?\n]',query) if x.strip()]
    covered=0
    for clause in clauses:
        dish_scope=bool(re.search(r'这道菜|这次|当前菜|本菜|这一道',clause))
        global_scope=bool(re.search(r'我|一直|以后|所有菜|每道菜|本次会话|整个会话|全局',clause)) and not dish_scope
        scope='dish' if dish_scope else 'session' if global_scope else 'dish' if current_name else 'unknown'
        prefix=r'(?:(?:我|本人|一直|以后|所有菜|每道菜|本次会话|整个会话|全局|这道菜|这次|当前菜|本菜|这一道|都|的|请|要求|改成|按|做|要|现在)\s*)*'
        body=re.sub('^'+prefix,'',clause)
        body=re.sub(r'^(取消|清除)(?:这道菜|当前菜|本菜|本次会话|全局)的?',r'\1',body)
        changes=[]
        exclusion=None
        # Scoped clear is explicit. A generic "取消限制" is intentionally ambiguous.
        if re.fullmatch(r'(?:取消|清除|忽略)(?:这道菜的?|当前菜的?|本菜的?)(?:所有)?(?:忌口|限制|条件|要求)',clause):
            changes=[Change('clear','dish',source=clause)]
        elif re.fullmatch(r'(?:取消|清除)(?:本次会话的?|整个会话的?|全局)(?:所有)?(?:忌口|限制|条件|要求)',clause):
            changes=[Change('clear','session',source=clause)]
        elif re.fullmatch(r'(?:取消|清除|忽略)(?:所有)?(?:忌口|限制|条件|要求)',clause):
            result.ambiguous=True;covered+=1;continue
        else:
            allergy=re.fullmatch(r'(?:对)?([\u4e00-\u9fff]{1,12})过敏',body)
            exclusion=re.match(r'(?:不放|不吃|不要(?:放|吃)?|不能吃|不含)([\u4e00-\u9fff]{1,12}?)(?=的|$)',body)
            cancel=re.fullmatch(r'(?:取消|清除)(?:不放|不吃|不要(?:放|吃)?|不能吃|不含)([\u4e00-\u9fff]{1,12}?)(?:的要求|的限制|要求|限制)?',body)
            time=re.fullmatch(r'(?:控制在|不超过|最多|限时)([1-9][0-9]{0,2})分钟(?:内|以内)?',body)
            servings=re.fullmatch(r'([1-9][0-9]?|一|二|两|三|四|五|六|七|八|九|十)(?:人份|份)',body)
            if allergy:
                changes=[Change('set','session','allergy',allergy[1],clause)]
            elif cancel:
                changes=[Change('remove',scope,'exclude',cancel[1],clause)]
            elif exclusion:
                value=exclusion[1]
                changes=[Change('set',scope,'exclude',value,clause)]
            elif time:
                changes=[Change('set',scope,'max_minutes',time[1],clause)]
            elif servings:
                changes=[Change('set',scope,'servings',servings[1],clause)]
        if changes:
            # Combined "不放X的菜名怎么做" is not a standalone update.
            is_full=exclusion is None or exclusion.end()==len(body)
            if any(c.scope=='unknown' for c in changes):result.ambiguous=True
            result.changes.extend(changes)
            if is_full:covered+=1
    result.only_update=bool(clauses) and covered==len(clauses)
    return result


@dataclass
class ConstraintState:
    session: dict = field(default_factory=dict)
    dish: dict = field(default_factory=dict)
    dish_id: str = None
    hydrated: bool = False

    def bind(self, recipe_id):
        if recipe_id != self.dish_id:
            self.dish.clear()
            self.dish_id=recipe_id

    def apply(self, changes, recipe_id=None):
        if any(c.scope=='dish' for c in changes):
            if not recipe_id:raise ValueError('Dish constraint requires confirmed target')
            self.bind(recipe_id)
        for change in changes:
            target=self.session if change.scope=='session' else self.dish
            if change.action=='clear':target.clear()
            elif change.action=='remove':target.pop((change.kind,change.value),None)
            elif change.action=='set':
                key=(change.kind,'') if change.kind in {'servings','max_minutes'} else (change.kind,change.value)
                target[key]={'kind':change.kind,'value':change.value,'source':change.source}

    def active(self, recipe_id=None):
        overridden={x['kind'] for x in self.dish.values()} if recipe_id==self.dish_id else set()
        values=[dict(x,scope='session') for x in self.session.values()
                if x['kind'] not in {'servings','max_minutes'} or x['kind'] not in overridden]
        if recipe_id and recipe_id==self.dish_id:
            values += [dict(x,scope='dish') for x in self.dish.values()]
        return values

    def any(self):
        return bool(self.session or self.dish)


def state_for(manager, session_id):
    states=getattr(manager,'constraint_states',None)
    if not isinstance(states,dict):
        states={};manager.constraint_states=states
    return states.setdefault(session_id,ConstraintState())


def constraint_text(items):
    labels={'allergy':'用户声明过敏食材','exclude':'不使用/不食用','servings':'份数','max_minutes':'最多用时（分钟）'}
    lines=[('会话要求' if x['scope']=='session' else '当前菜要求')+'：'+labels[x['kind']]+' '+x['value']+('过敏' if x['kind']=='allergy' else '') for x in items]
    return ('\n已生效的用户要求：\n'+'\n'.join(lines)+
        '\n这些是用户要求，不是菜谱原文。若与原方冲突，明确说明冲突，不能宣称原方已满足；'
        '不编造缺失步骤、替代配方或用时。份数调整须保留原文公式和适用条件。') if lines else ''
