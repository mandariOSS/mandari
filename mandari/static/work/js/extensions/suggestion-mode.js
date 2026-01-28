/**
 * TipTap Suggestion Mode Extensions
 *
 * Provides track-changes functionality for the motion editor:
 * - SuggestionInsert: Marks text that should be added
 * - SuggestionDelete: Marks text that should be removed
 * - SuggestionComment: Inline comment markers
 *
 * Compatible with TipTap 2.x
 *
 * @license AGPL-3.0-or-later
 */

// Ensure TipTap is loaded
if (typeof window.TipTap === 'undefined') {
    console.warn('TipTap not loaded. Suggestion extensions will be registered when TipTap is available.');
}

/**
 * Edit modes for the motion editor
 */
const MotionEditMode = {
    EDIT: 'edit',           // Full editing capabilities
    SUGGEST: 'suggest',     // Changes are tracked as suggestions
    COMMENT: 'comment',     // Only commenting allowed
    VIEW: 'view',           // Read-only view
};

/**
 * Suggestion Insert Mark
 *
 * Marks text that is suggested to be inserted.
 * Displays with green background and tracking info.
 */
const SuggestionInsert = {
    name: 'suggestionInsert',

    addOptions() {
        return {
            HTMLAttributes: {},
        };
    },

    addAttributes() {
        return {
            authorId: {
                default: null,
                parseHTML: element => element.getAttribute('data-author-id'),
                renderHTML: attributes => {
                    if (!attributes.authorId) return {};
                    return { 'data-author-id': attributes.authorId };
                },
            },
            authorName: {
                default: '',
                parseHTML: element => element.getAttribute('data-author-name'),
                renderHTML: attributes => {
                    if (!attributes.authorName) return {};
                    return { 'data-author-name': attributes.authorName };
                },
            },
            timestamp: {
                default: null,
                parseHTML: element => element.getAttribute('data-timestamp'),
                renderHTML: attributes => {
                    if (!attributes.timestamp) return {};
                    return { 'data-timestamp': attributes.timestamp };
                },
            },
            accepted: {
                default: null,
                parseHTML: element => {
                    const val = element.getAttribute('data-accepted');
                    if (val === 'true') return true;
                    if (val === 'false') return false;
                    return null;
                },
                renderHTML: attributes => {
                    if (attributes.accepted === null) return {};
                    return { 'data-accepted': attributes.accepted.toString() };
                },
            },
        };
    },

    parseHTML() {
        return [
            {
                tag: 'span[data-suggestion-type="insert"]',
            },
        ];
    },

    renderHTML({ HTMLAttributes }) {
        return [
            'span',
            {
                ...HTMLAttributes,
                'data-suggestion-type': 'insert',
                class: 'suggestion-insert bg-green-100 border-b-2 border-green-400 dark:bg-green-900/30 dark:border-green-600',
                title: HTMLAttributes['data-author-name']
                    ? `Eingefuegt von ${HTMLAttributes['data-author-name']}`
                    : 'Eingefuegt',
            },
            0,
        ];
    },
};

/**
 * Suggestion Delete Mark
 *
 * Marks text that is suggested to be deleted.
 * Displays with strikethrough and red background.
 */
const SuggestionDelete = {
    name: 'suggestionDelete',

    addOptions() {
        return {
            HTMLAttributes: {},
        };
    },

    addAttributes() {
        return {
            authorId: {
                default: null,
                parseHTML: element => element.getAttribute('data-author-id'),
                renderHTML: attributes => {
                    if (!attributes.authorId) return {};
                    return { 'data-author-id': attributes.authorId };
                },
            },
            authorName: {
                default: '',
                parseHTML: element => element.getAttribute('data-author-name'),
                renderHTML: attributes => {
                    if (!attributes.authorName) return {};
                    return { 'data-author-name': attributes.authorName };
                },
            },
            timestamp: {
                default: null,
                parseHTML: element => element.getAttribute('data-timestamp'),
                renderHTML: attributes => {
                    if (!attributes.timestamp) return {};
                    return { 'data-timestamp': attributes.timestamp };
                },
            },
            accepted: {
                default: null,
                parseHTML: element => {
                    const val = element.getAttribute('data-accepted');
                    if (val === 'true') return true;
                    if (val === 'false') return false;
                    return null;
                },
                renderHTML: attributes => {
                    if (attributes.accepted === null) return {};
                    return { 'data-accepted': attributes.accepted.toString() };
                },
            },
        };
    },

    parseHTML() {
        return [
            {
                tag: 'span[data-suggestion-type="delete"]',
            },
        ];
    },

    renderHTML({ HTMLAttributes }) {
        return [
            'span',
            {
                ...HTMLAttributes,
                'data-suggestion-type': 'delete',
                class: 'suggestion-delete bg-red-100 line-through text-red-700 dark:bg-red-900/30 dark:text-red-400',
                title: HTMLAttributes['data-author-name']
                    ? `Geloescht von ${HTMLAttributes['data-author-name']}`
                    : 'Geloescht',
            },
            0,
        ];
    },
};

/**
 * Suggestion Manager
 *
 * Utility class to manage suggestions in the editor.
 */
class SuggestionManager {
    constructor(editor, currentUser) {
        this.editor = editor;
        this.currentUser = currentUser;
        this.editMode = MotionEditMode.EDIT;
    }

    /**
     * Set the current edit mode
     */
    setEditMode(mode) {
        if (!Object.values(MotionEditMode).includes(mode)) {
            console.error(`Invalid edit mode: ${mode}`);
            return;
        }
        this.editMode = mode;
        this.editor.setEditable(mode !== MotionEditMode.VIEW);
    }

    /**
     * Get the current edit mode
     */
    getEditMode() {
        return this.editMode;
    }

    /**
     * Mark selected text as a suggestion insert
     */
    markAsInsert() {
        if (this.editMode !== MotionEditMode.SUGGEST) return;

        this.editor.chain().focus().setMark('suggestionInsert', {
            authorId: this.currentUser?.id,
            authorName: this.currentUser?.name || 'Unbekannt',
            timestamp: new Date().toISOString(),
        }).run();
    }

    /**
     * Mark selected text as a suggestion delete
     */
    markAsDelete() {
        if (this.editMode !== MotionEditMode.SUGGEST) return;

        this.editor.chain().focus().setMark('suggestionDelete', {
            authorId: this.currentUser?.id,
            authorName: this.currentUser?.name || 'Unbekannt',
            timestamp: new Date().toISOString(),
        }).run();
    }

    /**
     * Accept a suggestion at the current position
     */
    acceptSuggestion() {
        const { state } = this.editor;
        const { from, to } = state.selection;

        // Check for insert suggestions
        const insertMark = state.doc.rangeHasMark(from, to, state.schema.marks.suggestionInsert);
        if (insertMark) {
            // Remove the mark but keep the text
            this.editor.chain().focus().unsetMark('suggestionInsert').run();
            return;
        }

        // Check for delete suggestions
        const deleteMark = state.doc.rangeHasMark(from, to, state.schema.marks.suggestionDelete);
        if (deleteMark) {
            // Remove the marked text entirely
            this.editor.chain().focus().deleteSelection().run();
            return;
        }
    }

    /**
     * Reject a suggestion at the current position
     */
    rejectSuggestion() {
        const { state } = this.editor;
        const { from, to } = state.selection;

        // Check for insert suggestions
        const insertMark = state.doc.rangeHasMark(from, to, state.schema.marks.suggestionInsert);
        if (insertMark) {
            // Remove the text entirely
            this.editor.chain().focus().deleteSelection().run();
            return;
        }

        // Check for delete suggestions
        const deleteMark = state.doc.rangeHasMark(from, to, state.schema.marks.suggestionDelete);
        if (deleteMark) {
            // Remove the mark but keep the text
            this.editor.chain().focus().unsetMark('suggestionDelete').run();
            return;
        }
    }

    /**
     * Accept all suggestions in the document
     */
    acceptAllSuggestions() {
        // Remove all insert marks (keep text)
        this.editor.chain()
            .focus()
            .selectAll()
            .unsetMark('suggestionInsert')
            .run();

        // Find and remove all delete-marked text
        // This requires iterating through the document
        const { state } = this.editor;
        const deletePositions = [];

        state.doc.descendants((node, pos) => {
            if (node.marks.find(m => m.type.name === 'suggestionDelete')) {
                deletePositions.push({ from: pos, to: pos + node.nodeSize });
            }
        });

        // Delete in reverse order to maintain positions
        deletePositions.reverse().forEach(({ from, to }) => {
            this.editor.chain().focus().deleteRange({ from, to }).run();
        });
    }

    /**
     * Reject all suggestions in the document
     */
    rejectAllSuggestions() {
        // Remove all delete marks (keep text)
        this.editor.chain()
            .focus()
            .selectAll()
            .unsetMark('suggestionDelete')
            .run();

        // Find and remove all insert-marked text
        const { state } = this.editor;
        const insertPositions = [];

        state.doc.descendants((node, pos) => {
            if (node.marks.find(m => m.type.name === 'suggestionInsert')) {
                insertPositions.push({ from: pos, to: pos + node.nodeSize });
            }
        });

        // Delete in reverse order to maintain positions
        insertPositions.reverse().forEach(({ from, to }) => {
            this.editor.chain().focus().deleteRange({ from, to }).run();
        });
    }

    /**
     * Get all suggestions in the document
     */
    getAllSuggestions() {
        const suggestions = [];
        const { state } = this.editor;

        state.doc.descendants((node, pos) => {
            node.marks.forEach(mark => {
                if (mark.type.name === 'suggestionInsert' || mark.type.name === 'suggestionDelete') {
                    suggestions.push({
                        type: mark.type.name === 'suggestionInsert' ? 'insert' : 'delete',
                        text: node.text,
                        from: pos,
                        to: pos + node.nodeSize,
                        authorId: mark.attrs.authorId,
                        authorName: mark.attrs.authorName,
                        timestamp: mark.attrs.timestamp,
                    });
                }
            });
        });

        return suggestions;
    }

    /**
     * Get suggestion count
     */
    getSuggestionCount() {
        return this.getAllSuggestions().length;
    }
}

// Export for use in other modules
if (typeof window !== 'undefined') {
    window.MotionEditMode = MotionEditMode;
    window.SuggestionInsert = SuggestionInsert;
    window.SuggestionDelete = SuggestionDelete;
    window.SuggestionManager = SuggestionManager;
}

// Export for ES modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        MotionEditMode,
        SuggestionInsert,
        SuggestionDelete,
        SuggestionManager,
    };
}
