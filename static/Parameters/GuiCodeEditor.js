class GuiCodeEditor extends GuiParameter {
  constructor(param, div) {
    super(param, div);
    let self = this;
    this.editor = CodeMirror.fromTextArea(gid(`${param.pid}_code`), {
      lineNumbers: true, // Display line numbers
      mode: "python", // Set mode to Python
      theme: "dracula" // Set theme (you can change it)
    });

    this.save_button = gid(`${param.pid}_save_button`);
    this.submit_button = gid(`${param.pid}_submit_button`);
    this.sync_status = gid(`${param.pid}_sync_status`);

    // Auto-sync state - keeps backend Parameter.state fresh without firing
    // hot subscribers. Only the submit button propagates downstream.
    // `dirty` tracks unsynced edits (drives the debounce + status badge).
    // `unsubmitted` tracks edits since last submit (drives button styling)
    // - the two diverge whenever auto-sync flushes silently.
    this.dirty = false;
    this.unsubmitted = false;
    this.syncTimer = null;
    this.SYNC_DELAY = 750;

    this.save_button.addEventListener('click', function () { self.save_file() });
    this.submit_button.addEventListener('click', function () { self.send() });

    this.editor.on('change', (cm, change) => {
      // Ignore programmatic setValue (origin 'setValue') so backend pushes
      // don't immediately mark the editor dirty.
      if (change.origin === 'setValue') return;
      self.markDirty();
    });
    this.editor.on('blur', () => self.flush());

    this.setStatus('synced');
    this.setSubmitState(false);

    // Set initial and maximum height
    let initialHeight = 50; // Initial height in pixels
    let maxHeight = 800; // Maximum height in pixels

    // Make editor resizable
    this.editor.setSize(null, initialHeight);

    // Grow editor to fill container when grid gives extra space
    const editorContainer = gid(`${param.pid}_editor`);
    if (editorContainer) {
      const ro = new ResizeObserver(() => {
        const available = editorContainer.clientHeight - 6; // minus resize bar
        if (available > initialHeight) {
          self.editor.setSize(null, Math.min(available, maxHeight));
          ro.disconnect();
        }
      });
      ro.observe(editorContainer);
    }

    // Get the resize bar element
    this.resizeBar = gid(`${param.pid}_resize-bar`);
    // Function to handle mouse down on the resize bar
    this.resizeBar.addEventListener('mousedown', function (event) {
      event.preventDefault(); // Prevent text selection
      var startY = event.clientY;
      var startHeight = self.editor.getWrapperElement().clientHeight;

      // Function to handle mouse move while dragging
      function onMouseMove(event) {
        var delta = event.clientY - startY;
        var newHeight = startHeight + delta;
        newHeight = Math.min(Math.max(newHeight, initialHeight), maxHeight);
        self.editor.setSize(null, newHeight);
      }

      // Function to handle mouse up after dragging
      function onMouseUp() {
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
      }

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    });
    gid(`${param.pid}_file_input`).addEventListener('change', function (event) {
      const fileInput = event.target;

      if (fileInput.files.length > 0) {
        const selectedFile = fileInput.files[0];
        // Read the file content
        const reader = new FileReader();
        reader.onload = function (e) {
          const fileContent = e.target.result;
          self.editor.setValue(fileContent);
          self.markDirty();
        };
        reader.readAsText(selectedFile);
      }
    });
  }

  getHTML(param) {
    return `{{ html }}`
  }

  markDirty() {
    this.dirty = true;
    this.setStatus('dirty');
    this.setSubmitState(true);
    if (this.syncTimer) clearTimeout(this.syncTimer);
    this.syncTimer = setTimeout(() => this.flush(), this.SYNC_DELAY);
  }

  setSubmitState(unsubmitted) {
    this.unsubmitted = unsubmitted;
    if (!this.submit_button) return;
    if (unsubmitted) {
      this.submit_button.classList.remove('btn--muted');
      this.submit_button.classList.add('btn--primary');
    } else {
      this.submit_button.classList.remove('btn--primary');
      this.submit_button.classList.add('btn--muted');
    }
  }

  flush() {
    if (this.syncTimer) {
      clearTimeout(this.syncTimer);
      this.syncTimer = null;
    }
    if (!this.dirty) return;
    this.setStatus('syncing');
    const text = this.editor.getValue();
    hermes.send_json(this.pid, { cmd: 'sync', text });
    this.dirty = false;
  }

  setStatus(state) {
    if (!this.sync_status) return;
    const labels = {
      synced:     { text: '* synced',       color: 'var(--text-muted, #888)' },
      dirty:      { text: '* modified',     color: 'var(--warning, #d4a017)' },
      syncing:    { text: '* syncing...',     color: 'var(--info, #4a9eff)' },
      submitting: { text: '* submitting...',  color: 'var(--info, #4a9eff)' },
    };
    const cfg = labels[state] || { text: state, color: 'inherit' };
    this.sync_status.textContent = cfg.text;
    this.sync_status.style.color = cfg.color;
  }

  call(data) {
    // Backend -> frontend messages are JSON-wrapped envelopes:
    //   {cmd:'synced'}        ack of a sync/submit
    //   {cmd:'set', text:...} push content into the editor
    // Anything that fails JSON parse is treated as a legacy raw-text push.
    try {
      const msg = JSON.parse(data);
      if (msg && typeof msg === 'object' && msg.cmd) {
        if (msg.cmd === 'synced') {
          this.setStatus('synced');
          return;
        }
        if (msg.cmd === 'set') {
          this.editor.setValue(msg.text || '');
          this.dirty = false;
          this.setSubmitState(false);
          this.setStatus('synced');
          this.onContentSet();
          return;
        }
      }
    } catch (e) {
      // fall through
    }
    this.editor.setValue(data);
    this.dirty = false;
    this.setSubmitState(false);
    this.setStatus('synced');
    this.onContentSet();
  }

  // Hook for subclasses to react to backend-driven content updates
  // (i.e. setValue from `set`/legacy push). User typing does NOT call this.
  onContentSet() {}

  send() {
    if (this.syncTimer) {
      clearTimeout(this.syncTimer);
      this.syncTimer = null;
    }
    const text = this.editor.getValue();
    this.setStatus('submitting');
    hermes.send_json(this.pid, { cmd: 'submit', text });
    this.dirty = false;
    this.setSubmitState(false);
  }

  save_file() {
    // Prompt for a filename
    const fileName = prompt('Enter a filename: ', "filename.evzr");

    if (fileName) {
      const fileContent = this.editor.getValue();

      const blob = new Blob([fileContent], { type: 'text/plain' });
      const blobUrl = URL.createObjectURL(blob);

      const a = document.createElement('a');
      a.href = blobUrl;

      a.download = fileName;

      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);

      URL.revokeObjectURL(blobUrl);
    }
  }
}
