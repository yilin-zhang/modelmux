;;; modelmux-tests.el --- Tests for ModelMux -*- lexical-binding: t; -*-

(require 'ert)
(require 'modelmux)

(ert-deftest modelmux-buffer-text-uses-whole-buffer-without-region ()
  (with-temp-buffer
    (insert "whole buffer")
    (should (equal (modelmux--buffer-text) "whole buffer"))))

(ert-deftest modelmux-buffer-text-prefers-active-region ()
  (with-temp-buffer
    (transient-mark-mode 1)
    (insert "before selected after")
    (goto-char 8)
    (push-mark 16 t t)
    (should (equal (modelmux--buffer-text) "selected"))))

(ert-deftest modelmux-task-entries-contain-artifact ()
  (let ((modelmux--tasks
         (list (modelmux--make-task
                :id 1 :kind "tts" :profile "voice" :status 'completed
                :progress 100 :message "Completed" :artifact "/tmp/result.wav"))))
    (should (equal (aref (cadar (modelmux--task-entries)) 6) "result.wav"))))

(ert-deftest modelmux-tasks-mode-has-artifact-bindings ()
  (should (eq (lookup-key modelmux-tasks-mode-map (kbd "o"))
              #'modelmux-task-open-externally))
  (should (eq (lookup-key modelmux-tasks-mode-map (kbd "O"))
              #'modelmux-task-open-directory)))

(ert-deftest modelmux-progress-cell-renders-a-bar ()
  (should (equal (modelmux--progress-cell 50) "██████░░░░░░  50%")))

(ert-deftest modelmux-task-refresh-preserves-row-and-column ()
  (let* ((modelmux--tasks-buffer " *modelmux-test-tasks*")
         (task (modelmux--make-task
                :id 7 :kind "tts" :profile "voice" :status 'running
                :progress 10 :message "Running"))
         (modelmux--tasks (list task)))
    (unwind-protect
        (with-current-buffer (get-buffer-create modelmux--tasks-buffer)
          (modelmux-tasks-mode)
          (tabulated-list-print t)
          (modelmux--goto-task-id "7")
          (move-to-column 9)
          (let ((column (current-column)))
            (setf (modelmux-task-progress task) 80)
            (modelmux--refresh-task-buffers)
            (should (equal (tabulated-list-get-id) "7"))
            (should (= (current-column) column))))
      (kill-buffer modelmux--tasks-buffer))))

;;; modelmux-tests.el ends here
