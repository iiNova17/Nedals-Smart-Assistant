const $ = id => document.getElementById(id);
let conversation = null;
let busy = false;
let ready = false;
const emptyView = $('messages').firstElementChild.cloneNode(true);
const initialTheme = localStorage.getItem('assistant-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
function setTheme(theme) { document.documentElement.dataset.theme = theme; $('theme').textContent = theme === 'dark' ? 'Light theme' : 'Dark theme'; localStorage.setItem('assistant-theme', theme); }
setTheme(initialTheme);
$('theme').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
function controls() { for (const id of ['send','upload','new','documents','help']) $(id).disabled = !ready || busy; }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, credentials: 'same-origin' });
  const content = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof content.detail === 'string' ? content.detail : `Request failed (${response.status}).`);
  return content;
}
function addMessage(role, text) {
  $('empty')?.remove();
  const element = document.createElement('article'); element.className = `message ${role}`;
  const name = document.createElement('small'); name.textContent = role === 'user' ? 'You' : $('brand').textContent;
  element.append(name);
  // A small safe Markdown subset: DOM nodes only, no model-supplied HTML.
  function inline(parent, line) {
    const pattern = /\[([^\]\n]+)\]\((https:\/\/[^\s<>]+)\)|https:\/\/[^\s<>]+|\*\*([^*]+)\*\*|`([^`]+)`/g;
    let previous = 0;
    for (const match of line.matchAll(pattern)) {
      parent.append(document.createTextNode(line.slice(previous, match.index)));
      if (match[3] || match[4]) {
        const mark = document.createElement(match[3] ? 'strong' : 'code'); mark.textContent = match[3] || match[4]; parent.append(mark);
      } else {
        const url = (match[2] || match[0]).replace(/[.,;]+$/, '');
        const link = document.createElement('a'); link.href = url; link.textContent = match[1] || url; link.target = '_blank'; link.rel = 'noopener noreferrer'; parent.append(link);
      }
      previous = match.index + match[0].length;
    }
    parent.append(document.createTextNode(line.slice(previous)));
  }
  for (const [index, line] of text.split('\n').entries()) {
    if (index) element.append(document.createTextNode('\n'));
    if (/^#{1,6} /.test(line)) { const heading=document.createElement('strong'); inline(heading,line.replace(/^#{1,6} /,'')); element.append(heading); }
    else inline(element,line.replace(/^[*-] /,'• '));
  }
  $('messages').append(element); $('messages').scrollTop = $('messages').scrollHeight;
}
async function fresh() { const data = await api('/api/v1/conversations', { method: 'POST' }); conversation = data.id; sessionStorage.setItem('assistant-conversation',conversation); $('messages').replaceChildren(emptyView.cloneNode(true)); $('notice').textContent = 'New conversation ready.'; }
async function send(text) {
  if (!ready || busy || !text.trim()) return;
  busy = true; controls(); $('notice').textContent = 'Thinking…'; addMessage('user', text);
  try { const result = await api(`/api/v1/conversations/${conversation}/messages`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text}) }); addMessage('assistant', result.text); $('notice').textContent = ''; }
  catch (error) { $('notice').textContent = error.message; }
  finally { busy = false; controls(); $('message').focus(); }
}
$('chat-form').onsubmit = event => { event.preventDefault(); const text = $('message').value; $('message').value = ''; void send(text); };
$('message').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('chat-form').requestSubmit(); } };
$('new').onclick = async () => { if (busy) return; try { await fresh(); } catch (error) { $('notice').textContent=error.message; } };
$('help').onclick = () => send('What can you help me with? Give me a few practical examples.');
$('documents').onclick = async () => { try { const data = await api('/api/v1/knowledge'); addMessage('assistant', data.disabled ? 'PDF indexing is not configured.' : (data.documents.map(d => `${d.name}: ${d.status} (${d.pages} pages)`).join('\n') || 'No PDFs discovered yet.') + (data.sync_error ? '\n'+data.sync_error : '')); } catch (error) { $('notice').textContent=error.message; } };
$('file').onchange = () => { $('filename').textContent=$('file').files[0]?.name || 'No file selected'; };
$('upload-form').onsubmit = async event => {
  event.preventDefault(); const file = $('file').files[0]; if (!file || busy) return;
  if (file.size > 25*1024*1024) { $('notice').textContent='Files must be 25 MiB or smaller.'; return; }
  busy=true; controls(); $('notice').textContent='Uploading to Drive…';
  try { const query=new URLSearchParams({filename:file.name,destination:$('folder').value}); const data=await api('/api/v1/drive/files?'+query,{method:'POST',body:file}); addMessage('assistant', `${data.duplicate ? 'Reused existing file' : 'Uploaded'}: ${data.file.name}\n${data.file.drive_url}\nIndex status: ${data.index_status}`); $('file').value=''; $('filename').textContent='No file selected'; $('notice').textContent=''; }
  catch(error) { $('notice').textContent=error.message; }
  finally { busy=false; controls(); }
};
async function start() {
  controls();
  try { await api('/api/v1/browser-session',{method:'POST'}); const me=await api('/api/v1/me'); $('brand').textContent=me.assistant_name; document.title=me.assistant_name; $('connection').textContent=`Ready · ${me.name}`; const existing=sessionStorage.getItem('assistant-conversation');
    if(existing) {
      try { const history=await api(`/api/v1/conversations/${existing}/messages`); conversation=existing; for(const m of history.messages) addMessage(m.role==='assistant'?'assistant':'user',m.text); }
      catch { await fresh(); }
    } else { await fresh(); }
    ready=true; controls(); }
  catch(error) { $('connection').textContent=error.message; }
}
void start();
