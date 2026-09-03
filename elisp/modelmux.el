;;; modelmux.el --- Run local AI models from Emacs -*- lexical-binding: t; -*-

;; Version: 0.4.0
;; Package-Requires: ((emacs "27.1"))
;; Keywords: processes, multimedia, tools

;;; Commentary:

;; A thin Emacs frontend for ModelMux's persistent run registry.

;;; Code:

(require 'cl-lib)
(require 'json)
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
  :type '(repeat string))

(defcustom modelmux-base-url "http://127.0.0.1:8765"
  "Base URL of the ModelMux gateway, without a trailing slash."
  :type 'string)

(defcustom modelmux-tts-profile "qwen3-tts-0.6b-base-8bit"
  "Profile used by `modelmux-speak'."
  :type 'string)

(defcustom modelmux-asr-profile "qwen3-asr-0.6b"
  "Profile used by `modelmux-transcribe'."
  :type 'string)

(defcustom modelmux-tasks-refresh-interval 1.0
  "Seconds between task refreshes while the task buffer is visible."
  :type 'number)

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
  '((t :inherit warning :weight bold :background reset))
  "Face for the mark indicator."
  :group 'modelmux)

(defvar modelmux--tasks nil)
(defconst modelmux--tasks-buffer "*ModelMux Tasks*")
(defvar-local modelmux--marked nil)
(defvar-local modelmux--refresh-timer nil)
(defvar-local modelmux--refresh-request nil)

(defun modelmux--buffer-text ()
  "Return the active region, or the entire current buffer."
  (if (use-region-p)
      (buffer-substring-no-properties (region-beginning) (region-end))
    (buffer-substring-no-properties (point-min) (point-max))))

;;;###autoload
(defun modelmux-speak ()
  "Generate speech for the active region or the entire current buffer."
  (interactive)
  (let ((text (string-trim (modelmux--buffer-text))))
    (when (string-empty-p text)
      (user-error "There is no text to read"))
    (modelmux--start-run "tts" text modelmux-tts-profile)))

(defun modelmux--file-base64 (file)
  "Return FILE contents encoded as single-line base64."
  (with-temp-buffer
    (set-buffer-multibyte nil)
    (insert-file-contents-literally file)
    (base64-encode-string (buffer-string) t)))

;;;###autoload
(defun modelmux-transcribe (audio-file)
  "Transcribe AUDIO-FILE using `modelmux-asr-profile'."
  (interactive
   (list (read-file-name "Audio file: " nil nil t nil #'file-regular-p)))
  (unless (file-regular-p audio-file)
    (user-error "Audio file does not exist: %s" audio-file))
  (modelmux--start-run "asr" (modelmux--file-base64 audio-file)
                       modelmux-asr-profile t))

(defun modelmux--url (path)
  (concat (string-remove-suffix "/" modelmux-base-url) path))

(defun modelmux--http-body-start ()
  (goto-char (point-min))
  (or (and (boundp 'url-http-end-of-headers) url-http-end-of-headers)
      (progn
        (re-search-forward "\r?\n\r?\n" nil t)
        (point))
      (point-min)))

(defun modelmux--http-json-from-current-buffer ()
  (let ((status (or (and (boundp 'url-http-response-status)
                         url-http-response-status)
                    0)))
    (goto-char (modelmux--http-body-start))
    (let ((payload
           (json-parse-buffer :object-type 'alist :array-type 'list
                              :null-object nil :false-object nil)))
      (when (>= status 400)
        (let* ((error-object (alist-get 'error payload))
               (message (and (listp error-object)
                             (alist-get 'message error-object))))
          (error "ModelMux: %s" (or message (format "HTTP %s" status)))))
      payload)))

(defun modelmux--http-json-sync (method path &optional payload)
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

(defun modelmux--http-json-async (method path payload callback)
  (let ((url-request-method method)
        (url-request-extra-headers '(("Content-Type" . "application/json")))
        (url-request-data (and payload (encode-coding-string
                                       (json-serialize payload) 'utf-8))))
    (url-retrieve
     (modelmux--url path)
     (lambda (status)
       (unwind-protect
           (condition-case error
               (if (plist-get status :error)
                   (message "ModelMux server is unavailable: %s"
                            (plist-get status :error))
                 (funcall callback (modelmux--http-json-from-current-buffer)))
             (error (message "%s" (error-message-string error))))
         (kill-buffer (current-buffer))))
     nil t t)))

(defun modelmux--server-command (action)
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

(defun modelmux--schedule-visible-refresh ()
  (when-let* ((buffer (get-buffer modelmux--tasks-buffer)))
    (when (get-buffer-window buffer t)
      (with-current-buffer buffer
        (when (derived-mode-p 'modelmux-tasks-mode)
          (modelmux-tasks-refresh))))))

(defun modelmux--start-run (task input profile &optional base64-encoded)
  "Submit TASK with INPUT and PROFILE directly to the ModelMux HTTP API.
When BASE64-ENCODED is non-nil, send INPUT as binary data encoded in base64."
  (modelmux--http-json-async
   "POST" "/v1/jobs"
   `((task . ,task) (model . ,profile)
     (,(if base64-encoded 'input_base64 'input) . ,input)
     (parameters . ,(make-hash-table :test 'equal)))
   (lambda (job)
     (modelmux--schedule-visible-refresh)
     (message "ModelMux %s job %s queued" (upcase task) (alist-get 'id job)))))

;;;###autoload
(defun modelmux-tasks ()
  "Show persistent ModelMux runs and their artifacts."
  (interactive)
  (pop-to-buffer (get-buffer-create modelmux--tasks-buffer))
  (unless (derived-mode-p 'modelmux-tasks-mode)
    (modelmux-tasks-mode))
  (modelmux-tasks-refresh))

(defun modelmux--task-by-id (id)
  (seq-find (lambda (task) (equal (alist-get 'id task) id)) modelmux--tasks))

(defun modelmux--task-at-point ()
  (when-let* ((id (tabulated-list-get-id)))
    (modelmux--task-by-id id)))

(defun modelmux--goto-task-id (id)
  (goto-char (point-min))
  (while (and (not (eobp))
              (not (equal (tabulated-list-get-id) id)))
    (forward-line 1))
  (equal (tabulated-list-get-id) id))

(defun modelmux--progress-cell (progress)
  (let* ((value (max 0 (min 100 (or progress 0))))
         (width 12)
         (filled (round (* width (/ value 100.0)))))
    (format "%s%s %3d%%"
            (make-string filled ?█)
            (make-string (- width filled) ?░)
            value)))

(defun modelmux--status-cell (status)
  (pcase status
    ("queued" (propertize "○ Queued" 'face 'modelmux-status-muted-face))
    ("running" (propertize "● Running" 'face 'modelmux-status-running-face))
    ("completed" (propertize "✓ Ready" 'face 'modelmux-status-success-face))
    ("failed" (propertize "✕ Failed" 'face 'modelmux-status-error-face))
    ("cancelled" (propertize "– Cancelled" 'face 'modelmux-status-muted-face))
    ("interrupted" (propertize "! Interrupted" 'face 'warning))
    (_ (or status "Unknown"))))

(defun modelmux--parse-time (value)
  (when (and value (not (string-empty-p value)))
    (ignore-errors (date-to-time value))))

(defun modelmux--duration-cell (task)
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
  (remove-overlays (point-min) (point-max) 'modelmux-mark t)
  (save-excursion
    (goto-char (point-min))
    (while (< (point) (point-max))
      (when-let* ((id (tabulated-list-get-id)))
        (when (gethash id modelmux--marked)
          (modelmux--add-mark-overlay)))
      (forward-line 1))))

(defun modelmux--add-mark-overlay ()
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
  (let (stale)
    (maphash (lambda (id _value)
               (unless (modelmux--task-by-id id) (push id stale)))
             modelmux--marked)
    (dolist (id stale) (remhash id modelmux--marked))))

(defun modelmux-tasks-refresh ()
  "Reload runs asynchronously and redraw the task table."
  (interactive)
  (unless (buffer-live-p modelmux--refresh-request)
    (let ((target (current-buffer)))
      (setq modelmux--refresh-request
            (modelmux--http-json-async
             "GET" "/v1/jobs" nil
             (lambda (tasks)
               (when (buffer-live-p target)
                 (with-current-buffer target
                   (setq modelmux--refresh-request nil
                         modelmux--tasks tasks)
                   (modelmux--prune-marks)
                   (modelmux--print-preserving-position)))))))))

(defun modelmux--timer-refresh (buffer)
  (when (and (buffer-live-p buffer) (get-buffer-window buffer t))
    (with-current-buffer buffer
      (when (derived-mode-p 'modelmux-tasks-mode)
        (modelmux-tasks-refresh)))))

(defun modelmux--task-id-at-point ()
  (or (tabulated-list-get-id) (user-error "No task at point")))

(defun modelmux--mark-region (beginning end)
  (let ((finish (if (and (> end beginning)
                         (save-excursion (goto-char end) (bolp)))
                    (1- end)
                  end)))
    (save-excursion
      (goto-char beginning)
      (beginning-of-line)
      (while (<= (line-beginning-position) finish)
        (when-let* ((id (tabulated-list-get-id)))
          (puthash id t modelmux--marked)
          (modelmux--add-mark-overlay))
        (forward-line 1)))))

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
  (let (ids)
    (maphash (lambda (id _value) (push id ids)) modelmux--marked)
    (nreverse ids)))

(defun modelmux--selected-task-ids ()
  (or (modelmux--marked-task-ids) (list (modelmux--task-id-at-point))))

(defun modelmux--download-artifact (callback)
  (let* ((task (or (modelmux--task-at-point) (user-error "No task at point")))
         (url (alist-get 'artifact_url task))
         (directory (expand-file-name
                     (alist-get 'id task)
                     (expand-file-name "modelmux/" temporary-file-directory)))
         (suffix (pcase (alist-get 'task task)
                   ("tts" ".wav") ("asr" ".txt") (_ ".artifact"))))
    (unless (equal (alist-get 'status task) "completed")
      (user-error "This task has no completed artifact"))
    (unless url (user-error "This task has no artifact URL"))
    (url-retrieve
     (modelmux--url url)
     (lambda (status)
       (unwind-protect
           (if (plist-get status :error)
               (message "Cannot download ModelMux artifact: %s"
                        (plist-get status :error))
             (let ((response-status url-http-response-status))
               (if (>= response-status 400)
                   (message "Cannot download ModelMux artifact: HTTP %s"
                            response-status)
                 (goto-char (modelmux--http-body-start))
                 (make-directory directory t)
                 (set-file-modes directory #o700)
                 (let ((path (expand-file-name (concat "artifact" suffix) directory)))
                   (let ((coding-system-for-write 'binary))
                     (write-region (point) (point-max) path nil 'silent))
                   (set-file-modes path #o600)
                   (funcall callback path)))))
         (kill-buffer (current-buffer))))
     nil t t)))

(defun modelmux-task-open-externally ()
  "Open the task artifact with the system default application."
  (interactive)
  (modelmux--download-artifact
   (lambda (path) (start-process "modelmux-open" nil "/usr/bin/open" path))))

(defun modelmux-task-open-directory ()
  "Open the task artifact's directory in Finder."
  (interactive)
  (modelmux--download-artifact
   (lambda (path)
     (start-process "modelmux-open-directory" nil "/usr/bin/open"
                    (file-name-directory path)))))

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
  (let ((ids (modelmux--selected-task-ids)))
    (when (yes-or-no-p (format "Delete %d run%s and managed artifact%s? "
                               (length ids)
                               (if (= (length ids) 1) "" "s")
                               (if (= (length ids) 1) "" "s")))
      (modelmux--http-json-sync "POST" "/v1/jobs/delete" `((ids . ,ids)))
      (clrhash modelmux--marked)
      (modelmux-tasks-refresh)
      (message "Deleted %d run%s" (length ids)
               (if (= (length ids) 1) "" "s")))))

(defun modelmux-task-cancel ()
  "Cancel marked tasks, or the task at point, through ModelMux."
  (interactive)
  (let ((ids (modelmux--selected-task-ids)))
    (modelmux--http-json-sync "POST" "/v1/jobs/cancel" `((ids . ,ids)))
    (modelmux-tasks-refresh)
    (message "Cancellation requested")))

(defun modelmux-stop ()
  "Cancel the first active ModelMux run."
  (interactive)
  (let* ((tasks (modelmux--http-json-sync "GET" "/v1/jobs"))
         (task (seq-find (lambda (item)
                           (member (alist-get 'status item) '("queued" "running")))
                         tasks)))
    (if task
        (progn
          (modelmux--http-json-sync
           "POST" (format "/v1/jobs/%s/cancel" (alist-get 'id task))
           (make-hash-table :test 'equal))
          (modelmux--schedule-visible-refresh)
          (message "Cancellation requested"))
      (message "No ModelMux task is active"))))

(defun modelmux--stop-refresh-timer ()
  (when (timerp modelmux--refresh-timer)
    (cancel-timer modelmux--refresh-timer))
  (setq modelmux--refresh-timer nil)
  (when (buffer-live-p modelmux--refresh-request)
    (kill-buffer modelmux--refresh-request))
  (setq modelmux--refresh-request nil))

(defvar modelmux-tasks-mode-map (make-sparse-keymap))
(set-keymap-parent modelmux-tasks-mode-map tabulated-list-mode-map)
(define-key modelmux-tasks-mode-map (kbd "RET") #'modelmux-task-open-externally)
(define-key modelmux-tasks-mode-map (kbd "o") #'modelmux-task-open-externally)
(define-key modelmux-tasks-mode-map (kbd "O") #'modelmux-task-open-directory)
(define-key modelmux-tasks-mode-map (kbd "k") #'modelmux-task-cancel)
(define-key modelmux-tasks-mode-map (kbd "e") #'modelmux-task-rename)
(define-key modelmux-tasks-mode-map (kbd "D") #'modelmux-task-delete)
(define-key modelmux-tasks-mode-map (kbd "m") #'modelmux-task-mark)
(define-key modelmux-tasks-mode-map (kbd "u") #'modelmux-task-unmark)
(define-key modelmux-tasks-mode-map (kbd "U") #'modelmux-tasks-unmark-all)
(define-key modelmux-tasks-mode-map (kbd "g") #'modelmux-tasks-refresh)

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
  (setq-local modelmux--refresh-timer
              (run-at-time modelmux-tasks-refresh-interval
                           modelmux-tasks-refresh-interval
                           #'modelmux--timer-refresh (current-buffer)))
  (add-hook 'kill-buffer-hook #'modelmux--stop-refresh-timer nil t))

(provide 'modelmux)

;;; modelmux.el ends here
