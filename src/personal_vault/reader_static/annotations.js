async function request(path, data) {
  const response = await fetch(path, data ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)} : {});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || result.message || "保存失败");
  return result;
}
function node(tag, text) { const el=document.createElement(tag); if(text) el.textContent=text; return el; }
function field(form, label, tag="input") {
  const wrap=node("label", label); const input=node(tag); wrap.append(input); form.append(wrap); return input;
}
function dialog(title) {
  const modal=node("dialog"); modal.className="annotation-dialog";
  const form=node("form"); const error=node("p"); error.setAttribute("role","status");
  const close=node("button","关闭"); close.type="button"; close.onclick=()=>modal.close();
  modal.append(node("h2",title),form,error,close); document.body.append(modal);
  modal.addEventListener("close",()=>modal.remove()); modal.showModal();
  return {modal,form,error};
}
export function addMessageActions(actions, message, title) {
  if(!message.source_id) return;
  const feedback=node("button","纠正"); feedback.type="button"; feedback.className="message-action";
  feedback.onclick=async()=>{
    const {modal,form,error}=dialog("纠正这条来源");
    const action=field(form,"来源状态","select");
    for(const [value,label] of [["outdated","已过时"],["quoted","这是引用材料"],["excluded","以后不使用"],["corrected","更正内容"],["restored","恢复使用"]]) {
      const option=node("option",label); option.value=value; action.append(option);
    }
    const kind=field(form,"陈述类型","select");
    for(const [value,label] of [["statement","一般陈述"],["preference","偏好"],["plan","计划"],["event","经历/事件"]]) {
      const option=node("option",label); option.value=value; kind.append(option);
    }
    const note=field(form,"说明或更正内容","textarea");
    const from=field(form,"适用开始日期"); from.type="date";
    const until=field(form,"适用结束日期"); until.type="date";
    const replacement=field(form,"替代消息 ID（可选）");
    const save=node("button","保存纠正"); save.type="submit"; form.append(save);
    try {
      const history=await request("/api/feedback?message_id="+encodeURIComponent(message.identity_key));
      const latest=history.items.at(-1);
      if(latest) { action.value=latest.action; kind.value=latest.assertion_kind; note.value=latest.note; from.value=latest.valid_from||""; until.value=latest.valid_until||""; replacement.value=latest.replacement_message_id||""; }
    } catch(e) {error.textContent=e.message;}
    form.onsubmit=async event=>{event.preventDefault();save.disabled=true;
      try {await request("/api/feedback",{message_id:message.identity_key,action:action.value,note:note.value,
        assertion_kind:kind.value,valid_from:from.value||null,valid_until:until.value||null,
        replacement_message_id:replacement.value||null}); feedback.textContent="已纠正";modal.close();}
      catch(e) {error.textContent=e.message;} finally {save.disabled=false;}
    };
  };
  const bookmark=node("button","收藏");bookmark.type="button";bookmark.className="message-action";
  bookmark.onclick=()=>{
    const {modal,form,error}=dialog("收藏到专题");
    const topic=field(form,"专题");const note=field(form,"备注","textarea");
    const save=node("button","保存收藏");save.type="submit";form.append(save);
    form.onsubmit=async event=>{event.preventDefault();try {await request("/api/bookmarks",{source_id:message.source_id,title,topic:topic.value,note:note.value});bookmark.textContent="已收藏";modal.close();}catch(e){error.textContent=e.message;}};
  };
  actions.append(feedback,bookmark);
}
export function addBookmarksButton(parent) {
  const sessions=node("button","Pi 会话");sessions.type="button";sessions.className="message-action";parent.append(sessions);
  sessions.onclick=async()=>{
    const {form,error}=dialog("Pi 会话");form.onsubmit=event=>event.preventDefault();
    try {const {items}=await request("/api/pi/sessions");
      for(const item of items.filter(x=>x.source_id)) {const p=node("p");const link=node("a",item.title);link.href="/?source="+encodeURIComponent(item.source_id);p.append(link);form.append(p);}
      if(!form.children.length)form.append(node("p","还没有归档的 Pi 会话。"));
    }catch(e){error.textContent=e.message;}
  };
  const button=node("button","收藏与专题");button.type="button";button.className="message-action";parent.append(button);
  button.onclick=async()=>{
    const {form,error}=dialog("收藏与专题");const topic=field(form,"筛选专题","select");const list=node("div");form.append(list);
    try {
      const {items}=await request("/api/bookmarks");
      const all=node("option","全部");all.value="";topic.append(all);
      for(const name of new Set(items.map(x=>x.topic).filter(Boolean))) {const option=node("option",name);option.value=name;topic.append(option);}
      function render() {list.replaceChildren();for(const item of items.filter(x=>!topic.value||x.topic===topic.value)) {
        const row=node("p");const link=node("a",item.title||"无标题会话");link.href="/?source="+item.source_id;
        const remove=node("button","移除");remove.type="button";remove.onclick=async()=>{try {await request("/api/bookmarks",{source_id:item.source_id,remove:true});items.splice(items.indexOf(item),1);render();}catch(e){error.textContent=e.message;}};
        row.append(link,node("span",item.topic?" · "+item.topic:""),remove,node("small",item.note));list.append(row);
      } if(!list.children.length) list.append(node("p","还没有收藏。"));}
      topic.onchange=render;render();
    } catch(e){error.textContent=e.message;}
    form.onsubmit=event=>event.preventDefault();
  };
}
let observer=null, timer=null;
export function trackPosition(root, messages) {
  observer?.disconnect();clearTimeout(timer);
  const ids=new Map(messages.filter(x=>x.message?.source_id).map(x=>[x.message.identity_key,x.message.source_id]));
  observer=new IntersectionObserver(entries=>{
    const visible=entries.filter(e=>e.isIntersecting).sort((a,b)=>a.boundingClientRect.top-b.boundingClientRect.top);
    const source=ids.get(visible[0]?.target.dataset.messageIdentityKey);if(!source)return;
    clearTimeout(timer);timer=setTimeout(()=>request("/api/position",{source_id:source}).catch(()=>{}),800);
  },{root,rootMargin:"0px 0px -60% 0px"});
  root.querySelectorAll("[data-message-identity-key]").forEach(el=>observer.observe(el));
}
export async function savedPosition(snapshot,conversation) {
  const query=new URLSearchParams({snapshot,conversation});
  return (await request("/api/position?"+query)).position?.message_id;
}
