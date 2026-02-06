"""
Markdown Template Filter für Django Templates

Konvertiert Markdown-Text zu sicherem HTML mit Tailwind CSS Styling.
Verwendet die Python markdown Bibliothek für korrekte Verarbeitung.
"""

import markdown
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(name="markdownify")
def markdownify(text):
    """
    Konvertiert Markdown zu HTML mit Tailwind CSS Styling.
    """
    if not text:
        return ""

    # Markdown zu HTML konvertieren mit Extensions
    md = markdown.Markdown(
        extensions=[
            "tables",  # Tabellen-Support
            "fenced_code",  # Code-Blöcke mit ```
            "nl2br",  # Newlines zu <br>
            "sane_lists",  # Bessere Listen-Verarbeitung
            "smarty",  # Typografische Anführungszeichen
        ]
    )

    html = md.convert(text)

    # Tailwind CSS Klassen zu HTML-Elementen hinzufügen
    # Headings
    html = html.replace("<h1>", '<h1 class="text-3xl font-bold text-gray-900 dark:text-white mt-10 mb-6">')
    html = html.replace("<h2>", '<h2 class="text-2xl font-bold text-gray-900 dark:text-white mt-10 mb-4">')
    html = html.replace("<h3>", '<h3 class="text-xl font-bold text-gray-900 dark:text-white mt-8 mb-4">')
    html = html.replace("<h4>", '<h4 class="text-lg font-semibold text-gray-900 dark:text-white mt-6 mb-3">')
    html = html.replace("<h5>", '<h5 class="text-base font-semibold text-gray-900 dark:text-white mt-6 mb-3">')
    html = html.replace("<h6>", '<h6 class="text-base font-medium text-gray-900 dark:text-white mt-4 mb-2">')

    # Paragraphs
    html = html.replace("<p>", '<p class="text-gray-600 dark:text-gray-300 leading-relaxed my-4">')

    # Lists (mit Einrückung)
    html = html.replace("<ul>", '<ul class="my-6 ml-4 space-y-3 list-none">')
    html = html.replace("<ol>", '<ol class="my-6 ml-4 space-y-3 list-none counter-reset-item">')
    html = html.replace(
        "<li>",
        '<li class="flex items-start gap-3 text-gray-600 dark:text-gray-300"><span class="w-2 h-2 bg-primary-500 rounded-full mt-2 flex-shrink-0"></span><span>',
    )
    html = html.replace("</li>", "</span></li>")

    # Tables
    html = html.replace(
        "<table>",
        '<div class="overflow-x-auto my-8"><table class="min-w-full border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">',
    )
    html = html.replace("</table>", "</table></div>")
    html = html.replace("<thead>", '<thead class="bg-gray-50 dark:bg-gray-800">')
    html = html.replace("<tbody>", '<tbody class="divide-y divide-gray-200 dark:divide-gray-700">')
    html = html.replace("<tr>", '<tr class="hover:bg-gray-50 dark:hover:bg-gray-800/50">')
    html = html.replace(
        "<th>",
        '<th class="px-4 py-3 text-left text-sm font-semibold text-gray-900 dark:text-white">',
    )
    html = html.replace("<td>", '<td class="px-4 py-3 text-sm text-gray-600 dark:text-gray-300">')

    # Code
    html = html.replace(
        "<code>",
        '<code class="bg-gray-100 dark:bg-gray-800 text-primary-600 dark:text-primary-400 px-1.5 py-0.5 rounded text-sm font-mono">',
    )
    html = html.replace("<pre>", '<pre class="bg-gray-900 text-gray-100 rounded-xl p-4 overflow-x-auto my-6">')

    # Blockquotes
    html = html.replace(
        "<blockquote>",
        '<blockquote class="border-l-4 border-primary-500 bg-primary-50 dark:bg-primary-900/20 pl-4 pr-4 py-3 my-6 text-gray-700 dark:text-gray-300 rounded-r-lg">',
    )

    # Horizontal rules entfernen (nur Abstand behalten)
    html = html.replace("<hr>", '<div class="my-8"></div>')
    html = html.replace("<hr />", '<div class="my-8"></div>')

    # Links
    html = html.replace(
        "<a ",
        '<a class="text-primary-600 dark:text-primary-400 hover:underline font-medium" target="_blank" rel="noopener noreferrer" ',
    )

    # Strong/Bold
    html = html.replace("<strong>", '<strong class="font-semibold text-gray-900 dark:text-white">')

    # Emphasis/Italic
    html = html.replace("<em>", '<em class="italic text-gray-700 dark:text-gray-200">')

    return mark_safe(html)
