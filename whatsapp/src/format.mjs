/** Adapt model Markdown to WhatsApp without rewriting facts, URLs or code. */
export function formatWhatsApp(text) {
  return String(text).split(/(```[\s\S]*?```)/g).map((part, i) => {
    if (i % 2) return part;
    return part.split(/(`[^`\n]+`)/g).map((segment, j) => {
      if (j % 2) return segment;
      return segment
        .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '$1\n$2')
        .replace(/^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$/gm, (_, title) => `*${title.replace(/^\*+|\*+$/g, '')}*`)
        .replace(/\*\*([^\n]+?)\*\*/g, '*$1*')
        .replace(/__([^\n]+?)__/g, '*$1*')
        .replace(/^[ \t]*\*[ \t]+/gm, '- ')
        .replace(/^[ \t]*[-*_]{3,}[ \t]*$/gm, '');
    }).join('').replace(/\n{3,}/g, '\n\n');
  }).join('').trim();
}
