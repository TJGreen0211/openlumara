const UPLOAD_STORE = {
    files: [],
    dragCount: 0,

    get isDragging() {
        return this.dragCount > 0;
    },

    dragEnter() {
        this.dragCount++;
    },

    dragLeave(event, el) {
        if (event.relatedTarget && el.contains(event.relatedTarget)) return;
        this.dragCount = Math.max(0, this.dragCount - 1);
    },

    dragOver(event) {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
    },

    handleDrop(event) {
        event.preventDefault();
        const droppedFiles = event.dataTransfer.files;
        if (droppedFiles && droppedFiles.length > 0) {
            this.files.push(...droppedFiles);
        }
        this.dragCount = 0;
    },

    addFile(event) {
        this.files.push(...event.target.files);
        if (this.files.length === 0) return;
        event.target.value = "";
    },

    removeFile(index) {
        this.files.splice(index, 1);
    },

    readFileAsBase64(file) {
        return new Promise((resolve) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result.split(",")[1]);
            reader.readAsDataURL(file);
        });
    },

    /**
     * Handle pasted images from clipboard
     * @param {ClipboardEvent} event - The paste event
     */
    async pasteImage(event) {
        const items = event.clipboardData?.items;
        if (!items) return;

        for (const item of items) {
            // Only handle image types
            if (item.type.startsWith('image/')) {
                const blob = item.getAsFile();
                if (blob) {
                    // Create a File object with a generated name, made unique
                    // because Date.now() collides when several images are
                    // pasted within the same millisecond
                    const timestamp = Date.now();
                    const ext = blob.type.split('/')[1] || 'png';
                    let name = `pasted-image-${timestamp}.${ext}`;
                    let n = 1;
                    while (this.files.some(f => f.name === name)) {
                        name = `pasted-image-${timestamp}-${n++}.${ext}`;
                    }
                    const file = new File([blob], name, {
                        type: blob.type
                    });
                    this.files.push(file);
                }
            }
        }
    },

    clear() {
        this.files = [];
        this.processed = [];
    }
};
