/**
 * Markdown Editor Initialisierung für Django Admin
 * Verwendet EasyMDE für ein angenehmes Markdown-Editing-Erlebnis
 */

document.addEventListener('DOMContentLoaded', function() {
    // Finde alle Markdown-Editor Textareas
    const textareas = document.querySelectorAll('textarea.markdown-editor');

    textareas.forEach(function(textarea) {
        // Prüfe ob EasyMDE bereits initialisiert wurde
        if (textarea.easymde) return;

        // Initialisiere EasyMDE
        const easymde = new EasyMDE({
            element: textarea,
            spellChecker: false,
            autosave: {
                enabled: true,
                uniqueId: 'mandari-' + textarea.name,
                delay: 10000,
            },
            status: ['lines', 'words', 'cursor'],
            toolbar: [
                'bold', 'italic', 'heading', '|',
                'quote', 'unordered-list', 'ordered-list', '|',
                'link', 'image', 'table', '|',
                'preview', 'side-by-side', 'fullscreen', '|',
                'guide'
            ],
            placeholder: 'Schreibe deinen Inhalt hier...\n\nMarkdown wird unterstützt:\n- **fett** oder *kursiv*\n- # Überschriften\n- Listen mit - oder 1.\n- [Links](url) und ![Bilder](url)',
            previewRender: function(plainText) {
                // Einfaches Markdown-to-HTML Rendering für Preview
                return this.parent.markdown(plainText);
            },
            // Styling
            minHeight: '400px',
        });

        // Speichere Referenz
        textarea.easymde = easymde;
    });
});

// Stelle sicher, dass der Editor-Inhalt beim Speichern synchronisiert wird
document.addEventListener('submit', function(e) {
    const textareas = document.querySelectorAll('textarea.markdown-editor');
    textareas.forEach(function(textarea) {
        if (textarea.easymde) {
            textarea.value = textarea.easymde.value();
        }
    });
});
