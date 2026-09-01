;;; modelmux.el --- Run local AI models from Emacs -*- lexical-binding: t; -*-

;; Version: 0.2.0
;; Package-Requires: ((emacs "27.1"))
;; Keywords: processes, multimedia, tools

;;; Commentary:

;; A thin Emacs frontend for ModelMux, with a local task queue and task UI.

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

(cl-defstruct (modelmux-task (:constructor modelmux--make-task))
  id kind profile status progress message artifact error input callback
  process error-process output-buffer stderr-fragment started-at)

(defvar modelmux--tasks nil)
(defvar modelmux--next-task-id 0)
(defconst modelmux--tasks-buffer "*ModelMux Tasks*")

(defun modelmux--buffer-text ()
  "Return the active region, or the entire current buffer."
  (if (use-region-p)
      (buffer-substring-no-properties (region-beginning) (region-end))
    (buffer-substring-no-properties (point-min) (point-max))))

;;;###autoload
(defun modelmux-speak ()
  "Read the active region, or the entire current buffer, with ModelMux."
  (interactive)
  (let ((text (string-trim (modelmux--buffer-text))))
    (when (string-empty-p text)
      (user-error "There is no text to read"))
    (modelmux--enqueue "tts" text modelmux-tts-profile
                       #'modelmux--open-artifact)))

(defun modelmux--enqueue (kind input profile callback)
  (let ((task (modelmux--make-task
               :id (cl-incf modelmux--next-task-id)
               :kind kind
               :profile profile
               :status 'queued
               :progress 0
               :message "Waiting"
               :input input
               :callback callback
               :stderr-fragment "")))
    (setq modelmux--tasks (append modelmux--tasks (list task)))
    (modelmux--refresh-task-buffers)
    (modelmux--start-next)
    (message "ModelMux task #%d queued" (modelmux-task-id task))
    task))

(defun modelmux--running-p ()
  (seq-some (lambda (task)
              (process-live-p (modelmux-task-process task)))
            modelmux--tasks))

(defun modelmux--start-next ()
  (unless (modelmux--running-p)
    (when-let* ((task (seq-find (lambda (item)
                                (eq (modelmux-task-status item) 'queued))
                              modelmux--tasks)))
      (modelmux--start-task task))))

(defun modelmux--start-task (task)
  (let* ((output-buffer (generate-new-buffer
                         (format " *modelmux-output-%d*" (modelmux-task-id task))))
         (error-process
          (make-pipe-process
           :name (format "modelmux-events-%d" (modelmux-task-id task))
           :noquery t
           :filter (lambda (_process chunk)
                     (modelmux--handle-event-chunk task chunk))))
         (arguments
          (append modelmux-command
                  (list (modelmux-task-kind task) "-" "--json" "--json-events")
                  (when (modelmux-task-profile task)
                    (list "--profile" (modelmux-task-profile task))))))
    (setf (modelmux-task-status task) 'running
          (modelmux-task-message task) "Starting"
          (modelmux-task-started-at task) (current-time)
          (modelmux-task-output-buffer task) output-buffer
          (modelmux-task-error-process task) error-process
          (modelmux-task-process task)
          (make-process
           :name (format "modelmux-%d" (modelmux-task-id task))
           :command arguments
           :buffer output-buffer
           :stderr error-process
           :connection-type 'pipe
           :noquery t
           :sentinel (lambda (process _event)
                       (when (memq (process-status process) '(exit signal))
                         (modelmux--finish-task task process)))))
    (process-send-string (modelmux-task-process task) (modelmux-task-input task))
    (process-send-eof (modelmux-task-process task))
    (setf (modelmux-task-input task) nil)
    (modelmux--refresh-task-buffers)))

(defun modelmux--handle-event-chunk (task chunk)
  (let* ((text (concat (modelmux-task-stderr-fragment task) chunk))
         (complete (string-suffix-p "\n" text))
         (lines (split-string text "\n"))
         (tail (if complete "" (car (last lines)))))
    (setf (modelmux-task-stderr-fragment task) tail)
    (dolist (line (if complete lines (butlast lines)))
      (unless (string-empty-p line)
        (modelmux--handle-event-line task line)))))

(defun modelmux--handle-event-line (task line)
  (condition-case _error
      (let* ((event (json-parse-string line :object-type 'alist))
             (type (alist-get 'type event)))
        (pcase type
          ("started"
           (setf (modelmux-task-status task) 'running
                 (modelmux-task-message task) (or (alist-get 'message event) "Running")))
          ("progress"
           (setf (modelmux-task-progress task)
                 (round (or (alist-get 'progress event) 0))
                 (modelmux-task-message task) (or (alist-get 'message event) "Running")))
          ("result"
           (setf (modelmux-task-artifact task) (alist-get 'output event)))
          ("error"
           (setf (modelmux-task-error task) (alist-get 'message event)))))
    (error
     (setf (modelmux-task-error task)
           (string-join (delq nil (list (modelmux-task-error task) line)) "\n"))))
  (modelmux--refresh-task-buffers))

(defun modelmux--read-final-result (task)
  (when-let* ((buffer (modelmux-task-output-buffer task)))
    (when (buffer-live-p buffer)
      (with-current-buffer buffer
        (goto-char (point-min))
        (unless (= (point-min) (point-max))
          (json-parse-buffer :object-type 'alist))))))

(defun modelmux--finish-task (task process)
  (let ((cancelled (eq (modelmux-task-status task) 'cancelled)))
    (unless cancelled
      (if (= (process-exit-status process) 0)
          (condition-case error
              (let ((result (modelmux--read-final-result task)))
                (setf (modelmux-task-artifact task)
                      (or (alist-get 'output result) (modelmux-task-artifact task))
                      (modelmux-task-progress task) 100
                      (modelmux-task-status task) 'completed
                      (modelmux-task-message task) "Completed")
                (when (and (modelmux-task-callback task)
                           (modelmux-task-artifact task))
                  (funcall (modelmux-task-callback task)
                           (modelmux-task-artifact task))))
            (error
             (setf (modelmux-task-status task) 'failed
                   (modelmux-task-error task) (error-message-string error)
                   (modelmux-task-message task) "Invalid result")))
        (setf (modelmux-task-status task) 'failed
              (modelmux-task-message task) "Failed"
              (modelmux-task-error task)
              (or (modelmux-task-error task)
                  (modelmux-task-stderr-fragment task)
                  (format "Exit status %d" (process-exit-status process))))))
    (when-let* ((buffer (modelmux-task-output-buffer task)))
      (when (buffer-live-p buffer) (kill-buffer buffer)))
    (when-let* ((stderr (modelmux-task-error-process task)))
      (when (process-live-p stderr) (delete-process stderr)))
    (setf (modelmux-task-process task) nil
          (modelmux-task-error-process task) nil
          (modelmux-task-output-buffer task) nil)
    (modelmux--refresh-task-buffers)
    (modelmux--start-next)))

(defun modelmux--open-artifact (path)
  (unless (file-exists-p path)
    (user-error "Artifact no longer exists: %s" path))
  (if (member (downcase (or (file-name-extension path) ""))
              '("wav" "aiff" "aif" "mp3" "m4a" "flac"))
      (play-sound-file path)
    (find-file path)))

;;;###autoload
(defun modelmux-tasks ()
  "Show all ModelMux tasks and their artifacts."
  (interactive)
  (pop-to-buffer (get-buffer-create modelmux--tasks-buffer))
  (unless (derived-mode-p 'modelmux-tasks-mode)
    (modelmux-tasks-mode))
  (tabulated-list-print t))

(defun modelmux--task-by-id (id)
  (seq-find (lambda (task) (= (modelmux-task-id task) id)) modelmux--tasks))

(defun modelmux--task-at-point ()
  (when-let* ((id (tabulated-list-get-id)))
    (modelmux--task-by-id (string-to-number id))))

(defun modelmux--goto-task-id (id)
  (goto-char (point-min))
  (while (and (not (eobp))
              (not (equal (tabulated-list-get-id) id)))
    (forward-line 1))
  (equal (tabulated-list-get-id) id))

(defun modelmux--progress-cell (progress)
  (let* ((value (max 0 (min 100 progress)))
         (width 12)
         (filled (round (* width (/ value 100.0)))))
    (format "%s%s %3d%%"
            (make-string filled ?█)
            (make-string (- width filled) ?░)
            value)))

(defun modelmux--task-entries ()
  (mapcar
   (lambda (task)
     (list
      (number-to-string (modelmux-task-id task))
      (vector
       (number-to-string (modelmux-task-id task))
       (modelmux-task-kind task)
       (or (modelmux-task-profile task) "default")
       (symbol-name (modelmux-task-status task))
       (modelmux--progress-cell (modelmux-task-progress task))
       (or (modelmux-task-message task) "")
       (if-let* ((artifact (modelmux-task-artifact task)))
           (file-name-nondirectory artifact)
         ""))))
   (reverse modelmux--tasks)))

(defun modelmux--refresh-task-buffers ()
  (when-let* ((buffer (get-buffer modelmux--tasks-buffer)))
    (with-current-buffer buffer
      (when (derived-mode-p 'modelmux-tasks-mode)
        (let ((id (tabulated-list-get-id))
              (column (current-column))
              (point-before (point))
              (window-starts
               (mapcar (lambda (window) (cons window (window-start window)))
                       (get-buffer-window-list buffer nil t))))
          (tabulated-list-print t)
          (if id
              (modelmux--goto-task-id id)
            (goto-char (min point-before (point-max))))
          (move-to-column column)
          (dolist (entry window-starts)
            (when (window-live-p (car entry))
              (set-window-start (car entry) (cdr entry) t))))))))

(defun modelmux-task-open-artifact ()
  "Open the artifact belonging to the task at point."
  (interactive)
  (let ((task (modelmux--task-at-point)))
    (unless task (user-error "No task at point"))
    (unless (modelmux-task-artifact task)
      (user-error "This task has no artifact yet"))
    (modelmux--open-artifact (modelmux-task-artifact task))))

(defun modelmux--artifact-at-point ()
  (let ((task (modelmux--task-at-point)))
    (unless task (user-error "No task at point"))
    (unless (modelmux-task-artifact task)
      (user-error "This task has no artifact yet"))
    (let ((artifact (modelmux-task-artifact task)))
      (unless (file-exists-p artifact)
        (user-error "Artifact no longer exists: %s" artifact))
      artifact)))

(defun modelmux-task-open-externally ()
  "Open the task artifact with the macOS default application."
  (interactive)
  (start-process "modelmux-open" nil "/usr/bin/open"
                 (modelmux--artifact-at-point)))

(defun modelmux-task-open-directory ()
  "Open the task artifact's directory in Finder."
  (interactive)
  (start-process "modelmux-open-directory" nil "/usr/bin/open"
                 (file-name-directory (modelmux--artifact-at-point))))

(defun modelmux--cancel-task (task)
  (when (memq (modelmux-task-status task) '(queued running))
    (setf (modelmux-task-status task) 'cancelled
          (modelmux-task-message task) "Cancelled"
          (modelmux-task-input task) nil)
    (when (process-live-p (modelmux-task-process task))
      (delete-process (modelmux-task-process task)))
    (modelmux--refresh-task-buffers)
    (modelmux--start-next)))

(defun modelmux-task-cancel ()
  "Cancel the queued or running task at point."
  (interactive)
  (let ((task (modelmux--task-at-point)))
    (unless task (user-error "No task at point"))
    (modelmux--cancel-task task)))

(defun modelmux-stop ()
  "Cancel the current ModelMux task."
  (interactive)
  (if-let* ((task (seq-find (lambda (item)
                             (eq (modelmux-task-status item) 'running))
                           modelmux--tasks)))
      (modelmux--cancel-task task)
    (message "No ModelMux task is running")))

(defvar modelmux-tasks-mode-map (make-sparse-keymap))
(set-keymap-parent modelmux-tasks-mode-map tabulated-list-mode-map)
(define-key modelmux-tasks-mode-map (kbd "RET") #'modelmux-task-open-artifact)
(define-key modelmux-tasks-mode-map (kbd "o") #'modelmux-task-open-externally)
(define-key modelmux-tasks-mode-map (kbd "O") #'modelmux-task-open-directory)
(define-key modelmux-tasks-mode-map (kbd "k") #'modelmux-task-cancel)

(define-derived-mode modelmux-tasks-mode tabulated-list-mode "ModelMux Tasks"
  "Major mode for viewing ModelMux tasks."
  (setq tabulated-list-format
        [("ID" 5 t)
         ("Task" 8 t)
         ("Profile" 30 t)
         ("Status" 11 t)
         ("Progress" 18 nil)
         ("Detail" 28 nil)
         ("Artifact" 28 nil)])
  (setq tabulated-list-padding 2)
  (setq tabulated-list-entries #'modelmux--task-entries)
  (tabulated-list-init-header))

(provide 'modelmux)

;;; modelmux.el ends here
