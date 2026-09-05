;;; modelmux.el --- Run local AI models from Emacs -*- lexical-binding: t; -*-

;; Version: 0.4.0
;; Package-Requires: ((emacs "27.1"))
;; Keywords: processes, multimedia, tools

;;; Commentary:

;; A thin Emacs frontend for ModelMux's persistent run registry.  Emacs talks
;; to the local gateway over HTTP; the `modelmux' CLI is used only to start and
;; stop the detached server.
;;
;; Point Emacs at a running gateway and load this file:
;;
;;   (setq modelmux-base-url "http://127.0.0.1:8765")
;;   (require 'modelmux)
;;
;; Entry points: `modelmux-server-start', `modelmux-server-status' and
;; `modelmux-server-stop' manage the gateway; `modelmux-speak' reads the region
;; or buffer aloud; `modelmux-transcribe' streams an audio file for ASR;
;; `modelmux-stop' cancels the first active run; and `modelmux-tasks' opens a
;; live table of runs and their artifacts.
;;
;; In the task table, RET or `o' opens an artifact with the system default
;; application, `O' opens its directory, `e' renames, `C-c C-k' cancels, `m'/`u'/`U'
;; manage marks, `D' deletes the marked runs, and `g' refreshes.

;;; Code:

(require 'seq)
(require 'subr-x)
(require 'tabulated-list)
(require 'url)
(require 'url-http)

(defgroup modelmux nil
  "Run local AI models through ModelMux."
  :group 'external)

(defcustom modelmux-command '("modelmux")
  "Command used only to control the ModelMux server lifecycle."
  :type '(repeat string)
  :group 'modelmux)

(defcustom modelmux-base-url "http://127.0.0.1:8765"
  "Base URL of the ModelMux gateway, without a trailing slash."
  :type 'string
  :group 'modelmux)

(defcustom modelmux-upload-program "curl"
  "Curl-compatible program used for streaming ModelMux file transfers."
  :type 'string
  :group 'modelmux)

(defcustom modelmux-tts-profile "qwen3-tts-0.6b-base-8bit"
  "Profile used by `modelmux-speak'."
  :type 'string
  :group 'modelmux)

(defcustom modelmux-asr-profile "qwen3-asr-0.6b"
  "Profile used by `modelmux-transcribe'."
  :type 'string
  :group 'modelmux)

(defcustom modelmux-tasks-refresh-interval 1.0
  "Seconds between task refreshes while the task buffer is visible."
  :type 'number
  :group 'modelmux)

(defface modelmux-status-running-face
  '((t :inherit font-lock-keyword-face :weight bold))
  "Face for active ModelMux runs."
  :group 'modelmux)

(defface modelmux-status-success-face
  '((t :inherit success :weight bold))
  "Face for completed ModelMux runs."
  :group 'modelmux)

(defface modelmux-status-error-face
  '((t :inherit error :weight bold))
  "Face for failed ModelMux runs."
  :group 'modelmux)

(defface modelmux-status-muted-face
  '((t :inherit shadow))
  "Face for inactive ModelMux runs."
  :group 'modelmux)

(defface modelmux-mark-face
  '((((background dark)) (:background "DarkGoldenrod4"))
    (t (:background "LightYellow1")))
  "Face for marked ModelMux rows."
  :group 'modelmux)

(defface modelmux-mark-indicator-face
  '((t :inherit warning :weight bold :background unspecified))
  "Face for the mark indicator."
  :group 'modelmux)

(defconst modelmux--active-statuses '("queued" "running")
  "Run statuses that have not reached a final state.")

(defvar modelmux--tasks nil
  "Most recently fetched ModelMux task records.")
(defconst modelmux--tasks-buffer "*ModelMux Tasks*"
  "Name of the ModelMux task-list buffer.")
(defvar-local modelmux--marked nil
  "Hash table of marked task IDs in the current task-list buffer.")
(defvar-local modelmux--refresh-timer nil
  "Refresh timer for the current task-list buffer.")
;; `define-derived-mode' calls `kill-all-local-variables', which would otherwise
;; drop the timer without cancelling it when the mode is re-entered.
(put 'modelmux--refresh-timer 'permanent-local t)
(defvar-local modelmux--refresh-in-flight nil
  "Non-nil while a task refresh request is outstanding.")

(defun modelmux--buffer-text ()
  "Return the active region, or the entire current buffer."
  (if (use-region-p)
      (buffer-substring-no-properties (region-beginning) (region-end))
    (buffer-substring-no-properties (point-min) (point-max))))

;;;###autoload
(defun modelmux-speak ()
  "Generate speech for the active region or buffer, then show its task."
  (interactive)
  (let ((text (string-trim (modelmux--buffer-text))))
    (when (string-empty-p text)
      (user-error "There is no text to read"))
    (modelmux--submit-text-run "tts" modelmux-tts-profile text)
    (modelmux-tasks)))

;;;###autoload
(defun modelmux-transcribe (audio-file)
  "Transcribe AUDIO-FILE using `modelmux-asr-profile', then show its task."
  (interactive
   (list (read-file-name "Audio file: " nil nil t)))
  (unless (file-regular-p audio-file)
    (user-error "Not a regular audio file: %s" audio-file))
  (modelmux--submit-file-run "asr" modelmux-asr-profile
                             (expand-file-name audio-file))
  (modelmux-tasks))

(defun modelmux--url (path)
  "Resolve API PATH against `modelmux-base-url'."
  (concat (string-remove-suffix "/" modelmux-base-url) path))

(defun modelmux--http-body-start ()
  "Move point to the start of the current HTTP response body and return it."
  (goto-char (point-min))
  (goto-char (or (bound-and-true-p url-http-end-of-headers)
                 (if (re-search-forward "\r?\n\r?\n" nil t) (point) (point-min))))
  (point))

(defun modelmux--http-json-from-current-buffer ()
  "Parse a ModelMux JSON response from the current URL buffer."
  (let ((status (or (bound-and-true-p url-http-response-status) 0)))
    (modelmux--http-body-start)
    (let ((payload
           (json-parse-buffer :object-type 'alist :array-type 'list
                              :null-object nil :false-object nil)))
      (when (>= status 400)
        (let* ((error-object (alist-get 'error payload))
               (message (and (listp error-object)
                             (alist-get 'message error-object))))
          (user-error "ModelMux: %s" (or message (format "HTTP %s" status)))))
      payload)))

(defun modelmux--http-json-sync (method path &optional payload)
  "Send a synchronous JSON request using METHOD to PATH with PAYLOAD."
  (let* ((url-request-method method)
         (url-request-extra-headers '(("Content-Type" . "application/json")))
         (url-request-data (and payload (encode-coding-string
                                        (json-serialize payload) 'utf-8)))
         (buffer (condition-case error
                     (url-retrieve-synchronously (modelmux--url path) t t 5)
                   (error
                    (user-error "ModelMux server is unavailable: %s"
                                (error-message-string error))))))
    (unless buffer
      (user-error "ModelMux server is not running; use `modelmux-server-start'"))
    (unwind-protect
        (with-current-buffer buffer (modelmux--http-json-from-current-buffer))
      (kill-buffer buffer))))

(defun modelmux--http-json-async (method path payload callback &optional finally)
  "Send JSON PAYLOAD using METHOD to PATH, then call CALLBACK.
FINALLY, when given, runs after CALLBACK on every outcome."
  (let ((url-request-method method)
        (url-request-extra-headers '(("Content-Type" . "application/json")))
        (url-request-data (and payload (encode-coding-string
                                       (json-serialize payload) 'utf-8))))
    (condition-case error
        (url-retrieve
         (modelmux--url path)
         (lambda (status)
           (unwind-protect
               (condition-case callback-error
                   (if (plist-get status :error)
                       (message "ModelMux server is unavailable: %s"
                                (plist-get status :error))
                     (funcall callback (modelmux--http-json-from-current-buffer)))
                 (error (message "%s" (error-message-string callback-error))))
             (when finally (funcall finally))
             (kill-buffer (current-buffer))))
         nil t t)
      (error
       (when finally (funcall finally))
       (message "ModelMux server is unavailable: %s"
                (error-message-string error))))))

(defun modelmux--server-command (action)
  "Run the configured ModelMux server ACTION and return its output."
  (unless modelmux-command
    (user-error "`modelmux-command' is empty"))
  (with-temp-buffer
    (let ((status (apply #'process-file (car modelmux-command) nil t nil
                         (append (cdr modelmux-command)
                                 (list "server" action)))))
      (unless (and (integerp status) (zerop status))
        (user-error "%s" (string-trim (buffer-string))))
      (string-trim (buffer-string)))))

;;;###autoload
(defun modelmux-server-start ()
  "Start the persistent ModelMux gateway."
  (interactive)
  (message "%s" (modelmux--server-command "start")))

;;;###autoload
(defun modelmux-server-stop ()
  "Stop the ModelMux gateway and cancel active jobs."
  (interactive)
  (message "%s" (modelmux--server-command "stop")))

;;;###autoload
(defun modelmux-server-status ()
  "Report whether the ModelMux gateway is running."
  (interactive)
  (message "%s" (modelmux--server-command "status")))

(defun modelmux--refresh-if-visible (&optional buffer)
  "Refresh BUFFER, or the task buffer, when it is a visible task list."
  (when-let* ((target (or buffer (get-buffer modelmux--tasks-buffer))))
    (when (and (buffer-live-p target) (get-buffer-window target t))
      (with-current-buffer target
        (when (derived-mode-p 'modelmux-tasks-mode)
          (modelmux-tasks-refresh))))))

(defun modelmux--submit-text-run (task profile text)
  "Submit TEXT to PROFILE as an asynchronous TASK."
  (modelmux--http-json-async
   "POST" "/v1/jobs"
   `((task . ,task) (model . ,profile) (input . ,text)
     (parameters . ,(make-hash-table :test 'equal)))
   (lambda (job)
     (modelmux--refresh-if-visible)
     (message "ModelMux %s job %s queued" (upcase task) (alist-get 'id job)))))

(defun modelmux--upload-url (task profile)
  "Return the binary upload URL for TASK and PROFILE."
  (modelmux--url (format "/v1/jobs/upload?task=%s&model=%s"
                         (url-hexify-string task)
                         (url-hexify-string profile))))

(defun modelmux--upload-sentinel (process _event)
  "Handle completion of a ModelMux file upload PROCESS."
  (when (memq (process-status process) '(exit signal))
    (let ((buffer (process-buffer process))
          (task (process-get process 'modelmux-task)))
      (unwind-protect
          (if (zerop (process-exit-status process))
              (with-current-buffer buffer
                (goto-char (point-min))
                (condition-case error
                    (let ((job (json-parse-buffer :object-type 'alist)))
                      (modelmux--refresh-if-visible)
                      (message "ModelMux %s job %s queued"
                               (upcase task) (alist-get 'id job)))
                  (error (message "ModelMux upload returned an invalid reply: %s"
                                  (error-message-string error)))))
            (message "ModelMux upload failed: %s"
                     (string-trim
                      (with-current-buffer buffer (buffer-string)))))
        (when (buffer-live-p buffer)
          (kill-buffer buffer))))))

(defun modelmux--submit-file-run (task profile file)
  "Stream FILE to PROFILE as an asynchronous TASK."
  (let* ((program (or (executable-find modelmux-upload-program)
                      (user-error "Cannot find %s" modelmux-upload-program)))
         (buffer (generate-new-buffer " *modelmux-upload*"))
         (process
          (make-process
           :name "modelmux-upload"
           :buffer buffer
           :command
           (list program "--silent" "--show-error" "--fail-with-body"
                 "--request" "POST"
                 "--header" "Content-Type: application/octet-stream"
                 "--upload-file" file
                 (modelmux--upload-url task profile))
           :coding 'utf-8-unix
           :noquery t
           :sentinel #'modelmux--upload-sentinel)))
    (process-put process 'modelmux-task task)
    (message "Uploading %s to ModelMux…" (file-name-nondirectory file))))

;;;###autoload
(defun modelmux-tasks ()
  "Show persistent ModelMux runs and their artifacts."
  (interactive)
  (pop-to-buffer (get-buffer-create modelmux--tasks-buffer))
  (unless (derived-mode-p 'modelmux-tasks-mode)
    (modelmux-tasks-mode))
  (modelmux-tasks-refresh))

(defun modelmux--task-by-id (id)
  "Return the cached task whose ID is ID, or nil."
  (seq-find (lambda (task) (equal (alist-get 'id task) id)) modelmux--tasks))

(defun modelmux--task-at-point ()
  "Return the cached task displayed at point, or nil."
  (when-let* ((id (tabulated-list-get-id)))
    (modelmux--task-by-id id)))

(defun modelmux--goto-task-id (id)
  "Move point to task ID and return non-nil when found."
  (goto-char (point-min))
  (while (and (not (eobp))
              (not (equal (tabulated-list-get-id) id)))
    (forward-line 1))
  (equal (tabulated-list-get-id) id))

(defun modelmux--progress-cell (progress)
  "Render numeric PROGRESS as a compact progress bar."
  (let* ((value (max 0 (min 100 (or progress 0))))
         (width 12)
         (filled (round (* width (/ value 100.0)))))
    (format "%s%s %3d%%"
            (make-string filled ?█)
            (make-string (- width filled) ?░)
            value)))

(defun modelmux--status-cell (status)
  "Render task STATUS with its corresponding face."
  (pcase status
    ("queued" (propertize "○ Queued" 'face 'modelmux-status-muted-face))
    ("running" (propertize "● Running" 'face 'modelmux-status-running-face))
    ("completed" (propertize "✓ Ready" 'face 'modelmux-status-success-face))
    ("failed" (propertize "✕ Failed" 'face 'modelmux-status-error-face))
    ("cancelled" (propertize "– Cancelled" 'face 'modelmux-status-muted-face))
    ("interrupted" (propertize "! Interrupted" 'face 'warning))
    (_ (or status "Unknown"))))

(defun modelmux--parse-time (value)
  "Parse timestamp VALUE, returning nil when it is absent or invalid."
  (when (and value (not (string-empty-p value)))
    (ignore-errors (date-to-time value))))

(defun modelmux--duration-cell (task)
  "Return a compact elapsed-time string for TASK."
  (let* ((start (modelmux--parse-time
                 (or (alist-get 'started_at task) (alist-get 'created_at task))))
         (end (modelmux--parse-time (alist-get 'finished_at task)))
         (seconds (and start
                       (max 0 (round (float-time
                                      (time-subtract (or end (current-time)) start)))))))
    (cond
     ((null seconds) "")
     ((>= seconds 3600) (format "%dh%02dm" (/ seconds 3600) (% (/ seconds 60) 60)))
     ((>= seconds 60) (format "%dm%02ds" (/ seconds 60) (% seconds 60)))
     (t (format "%ds" seconds)))))

(defun modelmux--task-entries ()
  "Build `tabulated-list-entries' from cached ModelMux tasks."
  (mapcar
   (lambda (task)
     (list
      (alist-get 'id task)
      (vector
       (propertize (or (alist-get 'name task) (alist-get 'id task)) 'face 'bold)
       (upcase (or (alist-get 'task task) ""))
       (or (alist-get 'profile task) "default")
       (modelmux--status-cell (alist-get 'status task))
       (modelmux--progress-cell (alist-get 'progress task))
       (modelmux--duration-cell task))))
   modelmux--tasks))

(defun modelmux--apply-marks ()
  "Redraw overlays for marked tasks in the current buffer."
  (remove-overlays (point-min) (point-max) 'modelmux-mark t)
  (unless (zerop (hash-table-count modelmux--marked))
    (save-excursion
      (goto-char (point-min))
      (while (< (point) (point-max))
        (when-let* ((id (tabulated-list-get-id)))
          (when (gethash id modelmux--marked)
            (modelmux--add-mark-overlay)))
        (forward-line 1)))))

(defun modelmux--add-mark-overlay ()
  "Add a mark overlay to the task row at point."
  (let ((beginning (line-beginning-position))
        (end (line-end-position)))
    (remove-overlays beginning end 'modelmux-mark t)
    (let ((row (make-overlay beginning end))
          (indicator (make-overlay beginning (min (1+ beginning) end))))
      (overlay-put row 'modelmux-mark t)
      (overlay-put row 'face 'modelmux-mark-face)
      (overlay-put indicator 'modelmux-mark t)
      (overlay-put indicator 'display
                   (propertize "*" 'face 'modelmux-mark-indicator-face)))))

(defun modelmux--print-preserving-position ()
  "Redraw the task table while preserving point and window positions."
  (let ((id (tabulated-list-get-id))
        (column (current-column))
        (point-before (point))
        (window-starts
         (mapcar (lambda (window) (cons window (window-start window)))
                 (get-buffer-window-list (current-buffer) nil t))))
    (tabulated-list-print t)
    (if id
        (modelmux--goto-task-id id)
      (goto-char (min point-before (point-max))))
    (move-to-column column)
    (modelmux--apply-marks)
    (dolist (entry window-starts)
      (when (window-live-p (car entry))
        (set-window-start (car entry) (cdr entry) t)))))

(defun modelmux--prune-marks ()
  "Discard marks for tasks absent from the latest response."
  (unless (zerop (hash-table-count modelmux--marked))
    (let ((present (make-hash-table :test 'equal))
          stale)
      (dolist (task modelmux--tasks)
        (puthash (alist-get 'id task) t present))
      (maphash (lambda (id _value)
                 (unless (gethash id present) (push id stale)))
               modelmux--marked)
      (dolist (id stale) (remhash id modelmux--marked)))))

(defun modelmux--task-active-p (task)
  "Return non-nil when TASK has not reached a final status."
  (member (alist-get 'status task) modelmux--active-statuses))

(defun modelmux--accept-tasks (tasks)
  "Store TASKS and redraw the table when the display would change.
An unchanged list of finished runs renders identically, so it is left alone;
active runs are redrawn regardless because their elapsed time advances."
  (unless (and (equal tasks modelmux--tasks)
               (not (seq-some #'modelmux--task-active-p tasks)))
    (setq modelmux--tasks tasks)
    (modelmux--prune-marks)
    (modelmux--print-preserving-position)))

(defun modelmux-tasks-refresh ()
  "Reload runs asynchronously and redraw the task table."
  (interactive)
  (unless modelmux--refresh-in-flight
    (let ((target (current-buffer)))
      (setq modelmux--refresh-in-flight t)
      (modelmux--http-json-async
       "GET" "/v1/jobs" nil
       (lambda (tasks)
         (when (buffer-live-p target)
           (with-current-buffer target (modelmux--accept-tasks tasks))))
       (lambda ()
         (when (buffer-live-p target)
           (with-current-buffer target
             (setq modelmux--refresh-in-flight nil))))))))

(defun modelmux--task-id-at-point ()
  "Return the task ID at point or signal a user error."
  (or (tabulated-list-get-id) (user-error "No task at point")))

(defun modelmux--mark-region (beginning end)
  "Mark task rows between BEGINNING and END."
  (save-excursion
    (goto-char beginning)
    (beginning-of-line)
    (while (< (point) end)
      (when-let* ((id (tabulated-list-get-id)))
        (puthash id t modelmux--marked)
        (modelmux--add-mark-overlay))
      (forward-line 1))))

(defun modelmux-task-mark ()
  "Mark the current task or all tasks in the active region."
  (interactive)
  (if (use-region-p)
      (let ((beginning (region-beginning)) (end (region-end)))
        (modelmux--mark-region beginning end)
        (deactivate-mark)
        (goto-char end)
        (beginning-of-line)
        (forward-line 1))
    (puthash (modelmux--task-id-at-point) t modelmux--marked)
    (modelmux--add-mark-overlay)
    (forward-line 1)))

(defun modelmux-task-unmark ()
  "Unmark the task at point and move to the next row."
  (interactive)
  (remhash (modelmux--task-id-at-point) modelmux--marked)
  (remove-overlays (line-beginning-position) (line-end-position) 'modelmux-mark t)
  (forward-line 1))

(defun modelmux-tasks-unmark-all ()
  "Clear all task marks."
  (interactive)
  (clrhash modelmux--marked)
  (remove-overlays (point-min) (point-max) 'modelmux-mark t)
  (message "Cleared all marks"))

(defun modelmux--marked-task-ids ()
  "Return task IDs marked in the current task-list buffer."
  (let (ids)
    (maphash (lambda (id _value) (push id ids)) modelmux--marked)
    (nreverse ids)))

(defun modelmux--selected-task-ids ()
  "Return marked task IDs, or the task ID at point when none are marked."
  (or (modelmux--marked-task-ids) (list (modelmux--task-id-at-point))))

(defun modelmux--artifact-download-sentinel
    (process _event temporary path callback)
  "Finish PROCESS downloading TEMPORARY, move it to PATH, and call CALLBACK."
  (when (memq (process-status process) '(exit signal))
    (let ((buffer (process-buffer process)))
      (unwind-protect
          (if (and (zerop (process-exit-status process))
                   (file-regular-p temporary))
              (progn
                (rename-file temporary path t)
                (set-file-modes path #o600)
                (funcall callback path))
            (when (file-exists-p temporary) (delete-file temporary))
            (message "Cannot download ModelMux artifact: %s"
                     (string-trim
                      (with-current-buffer buffer (buffer-string)))))
        (when (buffer-live-p buffer) (kill-buffer buffer))))))

(defun modelmux--download-artifact (callback)
  "Download the artifact at point and invoke CALLBACK with its local path."
  (let* ((task (or (modelmux--task-at-point) (user-error "No task at point")))
         (url (alist-get 'artifact_url task))
         (directory (expand-file-name
                     (alist-get 'id task)
                     (expand-file-name "modelmux/" temporary-file-directory)))
         (suffix (pcase (alist-get 'task task)
                   ("tts" ".wav") ("asr" ".txt") (_ ".artifact")))
         (path (expand-file-name (concat "artifact" suffix) directory)))
    (unless (equal (alist-get 'status task) "completed")
      (user-error "This task has no completed artifact"))
    (unless url (user-error "This task has no artifact URL"))
    (if (file-regular-p path)
        (funcall callback path)
      (let* ((program (or (executable-find modelmux-upload-program)
                          (user-error "Cannot find %s" modelmux-upload-program)))
             (buffer (generate-new-buffer " *modelmux-download*"))
             (temporary (make-temp-name (concat path ".")))
             (sentinel (lambda (process event)
                         (modelmux--artifact-download-sentinel
                          process event temporary path callback))))
        (make-directory directory t)
        (set-file-modes directory #o700)
        (make-process
         :name "modelmux-download"
         :buffer buffer
         :command (list program "--silent" "--show-error"
                        "--fail-with-body" "--output" temporary
                        (modelmux--url url))
         :coding 'utf-8-unix
         :noquery t
         :sentinel sentinel)
        (message "Downloading ModelMux artifact…")))))

(defun modelmux--open-externally (path)
  "Open PATH with the system default application."
  (pcase system-type
    ('darwin (start-process "modelmux-open" nil "open" path))
    ('windows-nt (start-process "modelmux-open" nil "cmd.exe" "/c" "start" "" path))
    (_ (if-let* ((program (executable-find "xdg-open")))
           (start-process "modelmux-open" nil program path)
         (browse-url-of-file path)))))

(defun modelmux-task-open-externally ()
  "Open the task artifact with the system default application."
  (interactive)
  (modelmux--download-artifact #'modelmux--open-externally))

(defun modelmux-task-open-directory ()
  "Open the directory holding the task artifact."
  (interactive)
  (modelmux--download-artifact
   (lambda (path) (modelmux--open-externally (file-name-directory path)))))

(defun modelmux-task-rename ()
  "Rename the task at point without renaming its artifact."
  (interactive)
  (let* ((task (or (modelmux--task-at-point) (user-error "No task at point")))
         (id (alist-get 'id task))
         (name (read-string "Rename run: " (alist-get 'name task))))
    (modelmux--http-json-sync "PATCH" (format "/v1/jobs/%s" id)
                              `((name . ,name)))
    (modelmux-tasks-refresh)
    (message "Renamed run to %s" name)))

(defun modelmux-task-delete ()
  "Delete marked tasks, or the task at point, through ModelMux."
  (interactive)
  (let* ((ids (modelmux--selected-task-ids))
         (count (length ids))
         (plural (if (= count 1) "" "s")))
    (when (yes-or-no-p (format "Delete %d run%s and managed artifact%s? "
                               count plural plural))
      (modelmux--http-json-sync "POST" "/v1/jobs/delete" `((ids . ,(vconcat ids))))
      (clrhash modelmux--marked)
      (modelmux-tasks-refresh)
      (message "Deleted %d run%s" count plural))))

(defun modelmux-task-cancel ()
  "Cancel marked tasks, or the task at point, through ModelMux."
  (interactive)
  (let ((ids (modelmux--selected-task-ids)))
    (modelmux--http-json-sync "POST" "/v1/jobs/cancel" `((ids . ,(vconcat ids))))
    (modelmux-tasks-refresh)
    (message "Cancellation requested")))

;;;###autoload
(defun modelmux-stop ()
  "Cancel the first active ModelMux run."
  (interactive)
  (let* ((tasks (modelmux--http-json-sync "GET" "/v1/jobs"))
         (task (seq-find #'modelmux--task-active-p tasks)))
    (if task
        (progn
          (modelmux--http-json-sync
           "POST" (format "/v1/jobs/%s/cancel" (alist-get 'id task))
           (make-hash-table :test 'equal))
          (modelmux--refresh-if-visible)
          (message "Cancellation requested"))
      (message "No ModelMux task is active"))))

(defun modelmux--stop-refresh-timer ()
  "Stop refresh activity associated with the current task-list buffer."
  (when (timerp modelmux--refresh-timer)
    (cancel-timer modelmux--refresh-timer))
  (setq modelmux--refresh-timer nil
        modelmux--refresh-in-flight nil))

(defvar modelmux-tasks-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "RET") #'modelmux-task-open-externally)
    (define-key map (kbd "o") #'modelmux-task-open-externally)
    (define-key map (kbd "O") #'modelmux-task-open-directory)
    (define-key map (kbd "C-c C-k") #'modelmux-task-cancel)
    (define-key map (kbd "e") #'modelmux-task-rename)
    (define-key map (kbd "D") #'modelmux-task-delete)
    (define-key map (kbd "m") #'modelmux-task-mark)
    (define-key map (kbd "u") #'modelmux-task-unmark)
    (define-key map (kbd "U") #'modelmux-tasks-unmark-all)
    (define-key map (kbd "g") #'modelmux-tasks-refresh)
    map)
  "Keymap for `modelmux-tasks-mode'.")

(define-derived-mode modelmux-tasks-mode tabulated-list-mode "ModelMux Tasks"
  "Major mode for viewing and managing persistent ModelMux runs."
  (setq tabulated-list-format
        [("Name" 34 t)
         ("Task" 7 t)
         ("Model" 30 t)
         ("Status" 14 t)
         ("Progress" 18 nil)
         ("Time" 8 nil)])
  (setq tabulated-list-padding 2)
  (setq tabulated-list-entries #'modelmux--task-entries)
  (setq-local modelmux--marked (make-hash-table :test 'equal))
  (tabulated-list-init-header)
  (modelmux--stop-refresh-timer)
  (setq-local modelmux--refresh-timer
              (run-at-time modelmux-tasks-refresh-interval
                           modelmux-tasks-refresh-interval
                           #'modelmux--refresh-if-visible (current-buffer)))
  (add-hook 'kill-buffer-hook #'modelmux--stop-refresh-timer nil t))

(provide 'modelmux)

;;; modelmux.el ends here
