;;; modelmux.el --- Run local AI models from Emacs -*- lexical-binding: t; -*-

;; Version: 0.3.0
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

(defgroup modelmux nil
  "Run local AI models through ModelMux."
  :group 'external)

(defcustom modelmux-command '("modelmux")
  "Command and leading arguments used to invoke ModelMux."
  :type '(repeat string))

(defcustom modelmux-tts-profile "qwen3-tts-0.6b-base-8bit"
  "Profile used by `modelmux-speak'."
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
(defvar modelmux--job-processes nil)
(defconst modelmux--tasks-buffer "*ModelMux Tasks*")
(defvar-local modelmux--marked nil)
(defvar-local modelmux--refresh-timer nil)
(defvar-local modelmux--refresh-process nil)

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

(defun modelmux--call-json (&rest arguments)
  "Call ModelMux with ARGUMENTS and decode its JSON output."
  (unless modelmux-command
    (user-error "`modelmux-command' is empty"))
  (let ((stderr-file (make-temp-file "modelmux-stderr-")))
    (unwind-protect
        (with-temp-buffer
          (let ((status
                 (apply #'process-file
                        (car modelmux-command) nil (list t stderr-file) nil
                        (append (cdr modelmux-command) arguments))))
            (unless (and (integerp status) (zerop status))
              (let ((details
                     (string-trim
                      (with-temp-buffer
                        (insert-file-contents stderr-file)
                        (buffer-string)))))
                (user-error "ModelMux failed%s"
                            (if (string-empty-p details) "" (concat ": " details)))))
            (goto-char (point-min))
            (json-parse-buffer :object-type 'alist :array-type 'list
                               :null-object nil :false-object nil)))
      (delete-file stderr-file))))

(defun modelmux--schedule-visible-refresh ()
  (when-let* ((buffer (get-buffer modelmux--tasks-buffer)))
    (when (get-buffer-window buffer t)
      (with-current-buffer buffer
        (when (derived-mode-p 'modelmux-tasks-mode)
          (modelmux-tasks-refresh))))))

(defun modelmux--start-run (task input profile)
  "Start TASK with INPUT and PROFILE through the ModelMux CLI."
  (let* ((output-buffer (generate-new-buffer " *modelmux-output*"))
         (event-process
          (make-pipe-process
           :name "modelmux-events"
           :noquery t
           :filter (lambda (_process _chunk)
                     (modelmux--schedule-visible-refresh))))
         (process
          (make-process
           :name (format "modelmux-%s" task)
           :command (append modelmux-command
                            (list task "-" "--profile" profile
                                  "--json" "--json-events"))
           :buffer output-buffer
           :stderr event-process
           :connection-type 'pipe
           :noquery t
           :sentinel
           (lambda (process _event)
             (when (memq (process-status process) '(exit signal))
               (setq modelmux--job-processes
                     (delq process modelmux--job-processes))
               (when-let* ((stderr (process-get process 'modelmux-stderr)))
                 (when (process-live-p stderr) (delete-process stderr)))
               (when-let* ((buffer (process-buffer process)))
                 (when (buffer-live-p buffer) (kill-buffer buffer)))
               (modelmux--schedule-visible-refresh))))))
    (process-put process 'modelmux-stderr event-process)
    (push process modelmux--job-processes)
    (process-send-string process input)
    (process-send-eof process)
    (modelmux--schedule-visible-refresh)
    (message "ModelMux %s started" (upcase task))))

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
  (unless (process-live-p modelmux--refresh-process)
    (let* ((target (current-buffer))
           (output (generate-new-buffer " *modelmux-runs*"))
           (process
            (make-process
             :name "modelmux-runs"
             :command (append modelmux-command '("runs" "list" "--json"))
             :buffer output
             :connection-type 'pipe
             :noquery t
             :sentinel
             (lambda (process _event)
               (when (memq (process-status process) '(exit signal))
                 (unwind-protect
                     (when (buffer-live-p target)
                       (with-current-buffer target
                         (setq modelmux--refresh-process nil)
                         (if (zerop (process-exit-status process))
                             (condition-case error
                                 (with-current-buffer (process-buffer process)
                                   (goto-char (point-min))
                                   (let ((tasks
                                          (json-parse-buffer
                                           :object-type 'alist :array-type 'list
                                           :null-object nil :false-object nil)))
                                     (with-current-buffer target
                                       (setq modelmux--tasks tasks)
                                       (modelmux--prune-marks)
                                       (modelmux--print-preserving-position))))
                               (error
                                (message "Cannot refresh ModelMux: %s"
                                         (error-message-string error))))
                           (message "ModelMux refresh failed"))))
                   (when-let* ((buffer (process-buffer process)))
                     (when (buffer-live-p buffer) (kill-buffer buffer)))))))))
      (setq modelmux--refresh-process process))))

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

(defun modelmux--artifact-at-point ()
  (let ((task (modelmux--task-at-point)))
    (unless task (user-error "No task at point"))
    (unless (alist-get 'artifact task)
      (user-error "This task has no artifact yet"))
    (let ((artifact (alist-get 'artifact task)))
      (unless (file-exists-p artifact)
        (user-error "Artifact no longer exists: %s" artifact))
      artifact)))

(defun modelmux-task-open-externally ()
  "Open the task artifact with the system default application."
  (interactive)
  (start-process "modelmux-open" nil "/usr/bin/open"
                 (modelmux--artifact-at-point)))

(defun modelmux-task-open-directory ()
  "Open the task artifact's directory in Finder."
  (interactive)
  (start-process "modelmux-open-directory" nil "/usr/bin/open"
                 (file-name-directory (modelmux--artifact-at-point))))

(defun modelmux-task-rename ()
  "Rename the task at point without renaming its artifact."
  (interactive)
  (let* ((task (or (modelmux--task-at-point) (user-error "No task at point")))
         (id (alist-get 'id task))
         (name (read-string "Rename run: " (alist-get 'name task))))
    (modelmux--call-json "runs" "rename" id name "--json")
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
      (apply #'modelmux--call-json
             (append (list "runs" "delete") ids (list "--json")))
      (clrhash modelmux--marked)
      (modelmux-tasks-refresh)
      (message "Deleted %d run%s" (length ids)
               (if (= (length ids) 1) "" "s")))))

(defun modelmux-task-cancel ()
  "Cancel marked tasks, or the task at point, through ModelMux."
  (interactive)
  (let ((ids (modelmux--selected-task-ids)))
    (apply #'modelmux--call-json
           (append (list "runs" "cancel") ids (list "--json")))
    (modelmux-tasks-refresh)
    (message "Cancellation requested")))

(defun modelmux-stop ()
  "Cancel the first active ModelMux run."
  (interactive)
  (let* ((tasks (modelmux--call-json "runs" "list" "--json"))
         (task (seq-find (lambda (item)
                           (member (alist-get 'status item) '("queued" "running")))
                         tasks)))
    (if task
        (progn
          (modelmux--call-json "runs" "cancel" (alist-get 'id task) "--json")
          (modelmux--schedule-visible-refresh)
          (message "Cancellation requested"))
      (message "No ModelMux task is active"))))

(defun modelmux--stop-refresh-timer ()
  (when (timerp modelmux--refresh-timer)
    (cancel-timer modelmux--refresh-timer))
  (setq modelmux--refresh-timer nil)
  (when (process-live-p modelmux--refresh-process)
    (delete-process modelmux--refresh-process))
  (setq modelmux--refresh-process nil))

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
