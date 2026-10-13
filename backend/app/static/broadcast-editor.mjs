const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FORMATS = ['bold', 'italic', 'underline', 'strike', 'link', 'list'];
export const imageUrl = id => {
  if (!UUID.test(id || '')) throw new Error('图片引用无效。');
  return `/creators/broadcast/images/${id.toLowerCase()}`;
};
function safeLink(value) {
  if (typeof value !== 'string' || value.length > 2048 || /[\s\u0000-\u001f\u007f]/.test(value)) throw new Error('请输入完整的 https:// 或 http:// 链接。');
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('链接不受支持。');
  return value;
}
export function messageDocument(value = '') {
  if (typeof value === 'string') value = {version: 2, ops: [{insert: value.endsWith('\n') ? value : value + '\n'}]};
  if (!value || value.version !== 2 || Object.keys(value).some(key => !['version', 'ops'].includes(key)) || !Array.isArray(value.ops) || value.ops.length > 8000) throw new Error('消息格式无效。');
  const ops = []; let text = '', images = 0;
  for (const op of value.ops) {
    if (!op || typeof op !== 'object' || Object.keys(op).some(key => !['insert', 'attributes'].includes(key))) throw new Error('消息格式无效。');
    const attributes = {};
    if (Object.hasOwn(op, 'attributes')) {
      if (!op.attributes || typeof op.attributes !== 'object' || Array.isArray(op.attributes)) throw new Error('文字格式无效。');
      for (const key of Object.keys(op.attributes).sort()) {
        const attr = op.attributes[key];
        if (!FORMATS.includes(key)) throw new Error('文字格式不受支持。');
        if (['bold', 'italic', 'underline', 'strike'].includes(key)) {
          if (typeof attr !== 'boolean') throw new Error('文字格式无效。');
          if (attr) attributes[key] = true;
        } else if (key === 'link') attributes.link = safeLink(attr);
        else if (['bullet', 'ordered'].includes(attr) && typeof op.insert === 'string' && /^\n+$/.test(op.insert)) attributes.list = attr;
        else throw new Error('列表格式无效。');
      }
    }
    let insert = op.insert;
    if (typeof insert === 'string') {
      if (/\u0000|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(insert)) throw new Error('消息包含无效字符。');
      text += insert;
      if (!insert) continue;
    } else {
      if (!insert || Object.keys(insert).length !== 1 || !UUID.test(insert.image || '') || Object.keys(attributes).length) throw new Error('图片引用无效。');
      insert = {image: insert.image.toLowerCase()}; images++;
    }
    const next = {insert, ...(Object.keys(attributes).length ? {attributes} : {})};
    if (attributes.list) { for (const _line of insert) ops.push({...next, insert: '\n'}); continue; }
    const previous = ops.at(-1);
    if (typeof insert === 'string' && typeof previous?.insert === 'string' && !attributes.list
        && JSON.stringify(previous.attributes) === JSON.stringify(next.attributes)) previous.insert += insert;
    else ops.push(next);
  }
  if (Array.from(text.replace(/\n$/, '')).length > 4000 || images > 5) throw new Error(images > 5 ? '每条消息最多 5 张图片。' : '消息最多 4,000 字符。');
  const normalized = {version: 2, ops};
  if (new TextEncoder().encode(JSON.stringify(normalized)).length > 250000) throw new Error('消息格式过多，请清除部分格式。');
  return normalized;
}
export const messageKey = value => JSON.stringify(messageDocument(value));
export const hasMessage = value => messageDocument(value).ops.some(op => typeof op.insert !== 'string' || op.insert.trim());
export const imageCount = value => messageDocument(value).ops.filter(op => typeof op.insert !== 'string').length;

function blocks(value) {
  const result = []; let current = [], start = 0, offset = 0;
  for (const op of messageDocument(value).ops) {
    if (typeof op.insert !== 'string') {
      if (current.length) { result.push({ops: current, start}); current = []; }
      result.push({ops: [op], start: offset}); offset++; start = offset;
    } else for (const part of op.insert.match(/[^\n]+|\n/g) || []) {
      current.push({...op, insert: part}); offset += part.length;
      if (part === '\n') { result.push({ops: current, start}); current = []; start = offset; }
    }
  }
  if (current.length) result.push({ops: current, start});
  return result;
}
export function moveImage(value, position, direction) {
  const parts = blocks(value), index = parts.findIndex(part => part.start === position && typeof part.ops[0].insert !== 'string');
  let target = index + direction;
  while (target >= 0 && target < parts.length && parts[target].ops.every(op => op.insert === '\n')) target += direction;
  if (index < 0 || target < 0 || target >= parts.length) return messageDocument(value);
  const [part] = parts.splice(index, 1); parts.splice(target, 0, part);
  return messageDocument({version: 2, ops: parts.flatMap(item => item.ops)});
}

export function renderMessage(target, value) {
  target.replaceChildren(); let line = [], list = null;
  const finish = type => {
    if (type) {
      const tag = type === 'ordered' ? 'ol' : 'ul';
      if (!list || list.tagName.toLowerCase() !== tag) { list = document.createElement(tag); target.append(list); }
      const item = document.createElement('li'); item.append(...line); list.append(item);
    } else {
      list = null; const paragraph = document.createElement('p');
      paragraph.append(...(line.length ? line : [document.createElement('br')])); target.append(paragraph);
    }
    line = [];
  };
  for (const op of messageDocument(value).ops) {
    if (typeof op.insert !== 'string') {
      if (line.length) finish(); list = null;
      const image = document.createElement('img'); image.src = imageUrl(op.insert.image); image.alt = '消息图片'; image.loading = 'lazy';
      target.append(image); continue;
    }
    for (const part of op.insert.match(/[^\n]+|\n/g) || []) {
      if (part === '\n') { finish(op.attributes?.list); continue; }
      let text = document.createTextNode(part);
      for (const [key, tag] of [['bold', 'strong'], ['italic', 'em'], ['underline', 'u'], ['strike', 's']]) if (op.attributes?.[key]) {
        const wrapper = document.createElement(tag); wrapper.append(text); text = wrapper;
      }
      if (op.attributes?.link) {
        const link = document.createElement('a'); link.href = safeLink(op.attributes.link); link.target = '_blank'; link.rel = 'noopener noreferrer';
        link.append(text); text = link;
      }
      line.push(text);
    }
  }
  if (line.length) finish();
}

export function createEditor(container, {onChange, onState, authorizeMedia, uploadImage}) {
  const Quill = window.Quill;
  if (!Quill) throw new Error('编辑器暂时无法加载，请刷新。');
  const Delta = Quill.import('delta');
  class PrivateImage extends Quill.import('blots/block/embed') {
    static blotName = 'image'; static tagName = 'IMG'; static className = 'broadcast-editor-image';
    static create(id) {
      const image = super.create();
      if (UUID.test(id || '')) { image.dataset.assetId = id.toLowerCase(); image.src = imageUrl(id); }
      image.alt = '消息图片'; image.draggable = false; return image;
    }
    static value(image) { return UUID.test(image.dataset.assetId || '') ? image.dataset.assetId : undefined; }
  }
  Quill.register(PrivateImage, true);
  const toolbar = document.createElement('div'); toolbar.className = 'broadcast-editor-toolbar';
  for (const [format, label] of [['bold', '加粗'], ['italic', '斜体'], ['underline', '下划线'], ['strike', '删除线'], ['link', '添加链接'], ['list', '项目符号'], ['list', '编号列表'], ['clean', '清除格式']]) {
    const button = document.createElement('button'); button.type = 'button'; button.className = `ql-${format}`; button.setAttribute('aria-label', label); button.title = label;
    if (format === 'list') button.value = label === '项目符号' ? 'bullet' : 'ordered'; toolbar.append(button);
  }
  const emoji = document.createElement('select'); emoji.className = 'broadcast-emoji'; emoji.setAttribute('aria-label', '插入表情');
  emoji.add(new Option('☺', '')); for (const symbol of ['😊', '🎉', '❤️', '👍', '✨', '🔥', '📢', '🎁', '🙏', '✅', '★', '✓', '→', '•']) emoji.add(new Option(symbol, symbol));
  const imageButton = document.createElement('button'); imageButton.type = 'button'; imageButton.className = 'ql-image'; imageButton.setAttribute('aria-label', '添加图片'); imageButton.title = '添加图片'; toolbar.append(imageButton);
  const file = document.createElement('input'); file.type = 'file'; file.accept = 'image/png,image/jpeg,image/gif,image/webp'; file.multiple = true; file.hidden = true;
  const surface = document.createElement('div'); surface.className = 'broadcast-editor-surface';
  const feedback = document.createElement('div'); feedback.className = 'broadcast-editor-feedback'; feedback.setAttribute('role', 'status'); feedback.hidden = true;
  const actions = document.createElement('div'); actions.className = 'broadcast-image-actions'; actions.hidden = true;
  container.replaceChildren(toolbar, surface, file, actions, feedback);
  let uploading = false, failures = [], retryRange = null, enabled = true, selectedImage = null, valid = true;
  const quill = new Quill(surface, {theme: 'snow', placeholder: '输入消息…', formats: FORMATS.concat('image'), modules: {
    toolbar: {container: toolbar, handlers: {image: () => void chooseImage()}},
    uploader: {mimetypes: ['image/png', 'image/jpeg', 'image/gif', 'image/webp'], handler: (range, files) => void upload(files, range)},
    clipboard: {matchers: [['IMG', image => {
      const id = image.dataset.assetId;
      return UUID.test(id || '') && image.getAttribute('src') === imageUrl(id) ? new Delta().insert({image:id.toLowerCase()}) : new Delta();
    }], [Node.ELEMENT_NODE, (_node, delta) => new Delta(delta.ops.map(op => {
      const attributes = Object.fromEntries(Object.entries(op.attributes || {}).filter(([key]) => FORMATS.includes(key)));
      if (attributes.link) { try { safeLink(attributes.link); } catch { delete attributes.link; } }
      return {insert: op.insert, ...(Object.keys(attributes).length ? {attributes} : {})};
    }))]]},
  }});
  toolbar.append(emoji);
  const contents = () => messageDocument({version: 2, ops: quill.getContents().ops});
  quill.root.setAttribute('aria-label', container.dataset.label || '消息正文'); quill.root.setAttribute('role', 'textbox'); quill.root.setAttribute('aria-multiline', 'true');
  function update() {
    quill.enable(enabled && !uploading); for (const control of toolbar.querySelectorAll('button, select')) control.disabled = !enabled || uploading;
    try { contents(); valid = true; } catch (error) { valid = false; showError(error.message); }
    onState?.();
  }
  function showError(message) { feedback.hidden = false; feedback.replaceChildren(document.createTextNode(message)); }
  function clearError() { failures = []; retryRange = null; feedback.hidden = true; feedback.replaceChildren(); valid = true; update(); }
  function failed(message) {
    showError(message);
    for (const [label, callback] of [['重试', () => void upload(failures,retryRange || undefined)], ['忽略未上传图片', clearError]]) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-sm btn-ghost-secondary'; button.textContent = label;
      button.addEventListener('click', callback); feedback.append(button);
    }
  }
  async function chooseImage() {
    if (!enabled || uploading) return;
    try {
      if (!await authorizeMedia()) return;
      file.value = ''; file.click();
    } catch (error) { showError(error.message || '图片授权暂时无法核对，请重试。'); }
  }
  async function upload(values, range = quill.getSelection(true) || {index: quill.getLength() - 1, length: 0}) {
    if (!enabled || uploading) return;
    const files = Array.from(values);
    if (!files.length) return;
    failures = files; retryRange = null; uploading = true; feedback.hidden = false; feedback.textContent = '正在上传图片…'; update();
    let index = range.index;
    try {
      if (imageCount(contents()) + files.length > 5) throw new Error('每条消息最多 5 张图片。');
      if (files.some(item => !['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(item.type) || !item.size || item.size > 10 * 1024 * 1024)) throw new Error('请选择 10 MB 以内的 PNG、JPG、GIF 或 WebP 图片。');
      if (!await authorizeMedia()) return;
      for (const item of files) {
        const image = await uploadImage(item); imageUrl(image.id);
        quill.insertEmbed(index, 'image', image.id, 'api');
        const positions = [...quill.root.querySelectorAll(`img[data-asset-id="${image.id}"]`)].map(item => quill.getIndex(Quill.find(item))).filter(value => value >= index);
        if (!positions.length) throw new Error('图片未插入，请重试。');
        index = Math.min(...positions) + 1; failures = failures.slice(1);
      }
      quill.setSelection(index, 0, 'silent'); feedback.hidden = true; retryRange = null;
    } catch (error) { retryRange = {index,length:0}; failed(error.message || '图片未上传，请重试。'); }
    finally { uploading = false; update(); }
  }
  file.addEventListener('change', () => void upload(file.files));
  quill.root.addEventListener('click', event => {
    selectedImage?.classList.remove('is-selected'); selectedImage = event.target.closest('img[data-asset-id]');
    actions.hidden = !selectedImage; selectedImage?.classList.add('is-selected');
  });
  for (const [label, direction] of [['上移', -1], ['下移', 1], ['移除', 0]]) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-sm btn-ghost-secondary'; button.textContent = label;
    button.addEventListener('click', () => {
      if (!enabled || uploading || !selectedImage) return;
      const position = quill.getIndex(Quill.find(selectedImage));
      if (!direction) quill.deleteText(position, 1, 'user'); else quill.setContents(moveImage(contents(), position, direction).ops, 'user');
      selectedImage = null; actions.hidden = true;
    }); actions.append(button);
  }
  emoji.addEventListener('change', () => {
    if (!emoji.value) return; const range = quill.getSelection(true) || {index: quill.getLength() - 1, length: 0};
    quill.insertText(range.index, emoji.value, 'user'); quill.setSelection(range.index + emoji.value.length, 0, 'silent'); emoji.value = '';
  });
  quill.on('text-change', delta => { if (retryRange) retryRange.index = delta.transformPosition(retryRange.index); update(); onChange?.(); });
  return {
    get: contents,
    set(value) { quill.setContents(messageDocument(value).ops, 'silent'); selectedImage = null; actions.hidden = true; clearError(); },
    enable(value) { if (enabled !== value) { enabled = value; update(); } },
    blocked: () => uploading || failures.length > 0 || !valid,
    uploading: () => uploading,
    focus: () => quill.focus(),
  };
}
