import { marked } from "./vendor/marked.js";
import katex from "./vendor/katex.js";

marked.use({extensions: [
  {name:"mathBlock", level:"block", start:src=>src.search(/\$\$|\\\[/), tokenizer(src) {
    const m=/^(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])(?:\n|$)/.exec(src);
    if(m) return {type:"mathBlock",raw:m[0],text:m[1]||m[2]};
  }},
  {name:"mathInline", level:"inline", start:src=>src.search(/\$|\\\(/), tokenizer(src) {
    const m=/^(?:\$(?!\s)([^$\n]+?)\$|\\\(([\s\S]+?)\\\))/.exec(src);
    if(m) return {type:"mathInline",raw:m[0],text:m[1]||m[2]};
  }}
]});

function node(tag, text) {
  const result = document.createElement(tag);
  if (text != null) result.textContent = text;
  return result;
}

function tokensToDom(tokens) {
  const fragment = document.createDocumentFragment();
  for (const token of tokens || []) {
    let item;
    switch (token.type) {
      case "space": continue;
      case "mathBlock":
      case "mathInline": {
        const math = node(token.type === "mathBlock" ? "div" : "span");
        math.className = "math-formula";
        try { katex.render(token.text, math, {output:"mathml", displayMode:token.type === "mathBlock", trust:false}); }
        catch { math.textContent = token.raw; }
        fragment.append(math); continue;
      }
      case "heading": item = node(`h${Math.min(token.depth + 1, 6)}`); break;
      case "paragraph": item = node("p"); break;
      case "strong": item = node("strong"); break;
      case "em": item = node("em"); break;
      case "del": item = node("del"); break;
      case "blockquote": item = node("blockquote"); break;
      case "br": fragment.append(node("br")); continue;
      case "hr": fragment.append(node("hr")); continue;
      case "codespan": fragment.append(node("code", token.text)); continue;
      case "code": {
        const block = node("div"); block.className = "code-block";
        const header = node("div"); header.className = "code-toolbar";
        header.append(node("span", token.lang || "代码"));
        const copy = node("button", "复制代码"); copy.type = "button";
        copy.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(token.text); copy.textContent = "已复制"; }
          catch { copy.textContent = "复制失败"; }
        });
        header.append(copy);
        const pre = node("pre"); pre.append(node("code", token.text));
        block.append(header, pre); fragment.append(block); continue;
      }
      case "list": {
        item = node(token.ordered ? "ol" : "ul");
        if (token.ordered && token.start) item.start = token.start;
        for (const entry of token.items) {
          const li = node("li");
          if (entry.task) {
            const checkbox = node("input"); checkbox.type = "checkbox";
            checkbox.checked = entry.checked; checkbox.disabled = true; li.append(checkbox);
          }
          li.append(tokensToDom(entry.tokens)); item.append(li);
        }
        fragment.append(item); continue;
      }
      case "table": {
        const wrapper = node("div"); wrapper.className = "table-scroll";
        const table = node("table"); const head = node("thead"); const body = node("tbody");
        const addRow = (cells, tag) => {
          const row = node("tr");
          for (const cell of cells) {
            const td = node(tag); td.append(tokensToDom(cell.tokens)); row.append(td);
          }
          return row;
        };
        head.append(addRow(token.header, "th"));
        for (const row of token.rows) body.append(addRow(row, "td"));
        table.append(head, body); wrapper.append(table); fragment.append(wrapper); continue;
      }
      case "link": {
        item = node("a");
        try {
          const url = new URL(token.href);
          if (["http:", "https:", "mailto:"].includes(url.protocol)) {
            item.href = url.href; item.target = "_blank"; item.rel = "noreferrer noopener";
          }
        } catch { /* Non-web references remain readable text. */ }
        break;
      }
      case "image": fragment.append(node("span", token.text ? `[图片：${token.text}]` : "[图片]")); continue;
      case "text":
        fragment.append(token.tokens ? tokensToDom(token.tokens) : document.createTextNode(token.text || ""));
        continue;
      default:
        // Exported HTML is conversation text, never executable page markup.
        fragment.append(document.createTextNode(token.raw || token.text || "")); continue;
    }
    item.append(tokensToDom(token.tokens));
    fragment.append(item);
  }
  return fragment;
}

export function renderMarkdown(text) {
  return tokensToDom(marked.lexer(text, { ...marked.defaults, gfm: true, breaks: true }));
}
