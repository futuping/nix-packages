#import <Cocoa/Cocoa.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

// Finder sends Apple events, not argv. Keep this small event receiver alive
// while its Neomacs sessions run; upstream's winit delegate cannot receive
// Finder open-document events. Do not alter the user's Emacs server or init.
@interface NeomacsLauncher : NSObject <NSApplicationDelegate>
@property(nonatomic, strong) NSMutableArray<NSTask *> *sessions;
@property(nonatomic) BOOL receivedOpenRequest;
- (BOOL)startSession:(NSArray<NSString *> *)arguments;
- (BOOL)openFiles:(NSArray<NSString *> *)files;
- (BOOL)openUntitledIfNeeded;
- (void)terminateWhenIdle;
- (void)reportError:(NSError *)error;
@end

@implementation NeomacsLauncher
- (instancetype)init {
    self = [super init];
    if (self) {
        _sessions = [NSMutableArray array];
    }
    return self;
}

- (void)reportError:(NSError *)error {
    NSLog(@"Unable to launch Neomacs: %@", error.localizedDescription);
    [[NSAlert alertWithError:error] runModal];
}

- (void)terminateWhenIdle {
    // Let the current event reply finish, then recheck for a newer request.
    dispatch_async(dispatch_get_main_queue(), ^{
        if (self.sessions.count == 0) {
            [NSApp terminate:nil];
        }
    });
}

- (BOOL)startSession:(NSArray<NSString *> *)arguments {
    NSTask *session = [[NSTask alloc] init];
    session.executableURL = [NSURL fileURLWithPath:@"@neomacsProgram@"];
    session.arguments = arguments;
    session.standardInput = [NSFileHandle fileHandleWithNullDevice];
    __weak NeomacsLauncher *weakSelf = self;
    session.terminationHandler = ^(NSTask *finished) {
        dispatch_async(dispatch_get_main_queue(), ^{
            NeomacsLauncher *launcher = weakSelf;
            [launcher.sessions removeObject:finished];
            [launcher terminateWhenIdle];
        });
    };
    // Register before launch: a short-lived child may exit immediately.
    [self.sessions addObject:session];
    NSError *error = nil;
    if (![session launchAndReturnError:&error]) {
        [self.sessions removeObject:session];
        [self reportError:error];
        [self terminateWhenIdle];
        return NO;
    }
    return YES;
}

- (BOOL)openFiles:(NSArray<NSString *> *)files {
    self.receivedOpenRequest = YES;
    // Preserve spaces/Unicode and protect names beginning with '-' from option
    // parsing. Neither a shell command nor a Lisp expression is constructed.
    NSArray<NSString *> *arguments = files.count
        ? [@[@"--"] arrayByAddingObjectsFromArray:files]
        : @[];
    return [self startSession:arguments];
}

- (void)application:(NSApplication *)application openFiles:(NSArray<NSString *> *)files {
    BOOL opened = [self openFiles:files];
    [application replyToOpenOrPrint:opened ? NSApplicationDelegateReplySuccess
                                         : NSApplicationDelegateReplyFailure];
}

- (void)application:(NSApplication *)application openURLs:(NSArray<NSURL *> *)urls {
    NSMutableArray<NSString *> *files = [NSMutableArray array];
    for (NSURL *url in urls) {
        if (!url.isFileURL || !url.path) {
            self.receivedOpenRequest = YES;
            [self reportError:[NSError errorWithDomain:NSCocoaErrorDomain
                                                  code:NSFileReadUnsupportedSchemeError
                                              userInfo:@{NSURLErrorKey: url}]];
            [self terminateWhenIdle];
            return;
        }
        [files addObject:url.path];
    }
    (void)application;
    [self openFiles:files];
}

- (BOOL)openUntitledIfNeeded {
    if (!self.receivedOpenRequest && self.sessions.count == 0) {
        return [self openFiles:@[]];
    }
    return YES;
}

- (BOOL)applicationOpenUntitledFile:(NSApplication *)application {
    (void)application;
    return [self openUntitledIfNeeded];
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    // AppKit delivers the initial open-document event before this callback.
    // The shared guard prevents an extra empty session after a file launch.
    dispatch_async(dispatch_get_main_queue(), ^{
        [self openUntitledIfNeeded];
    });
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)application
                   hasVisibleWindows:(BOOL)visible {
    (void)application;
    (void)visible;
    NSTask *session = self.sessions.lastObject;
    if (session) {
        NSRunningApplication *running =
            [NSRunningApplication runningApplicationWithProcessIdentifier:session.processIdentifier];
        if (![running activateWithOptions:0]) {
            NSLog(@"Neomacs session %d could not be activated", session.processIdentifier);
        }
    } else {
        [self openFiles:@[]];
    }
    return NO;
}
@end

#ifndef NEOMACS_LAUNCHER_TEST
int main(int argc, char **argv) {
    // Keep direct CLI invocations (including headless package checks) exactly
    // upstream. Older LaunchServices may supply only its private -psn argument.
    if (argc > 1 && !(argc == 2 && strncmp(argv[1], "-psn_", 5) == 0)) {
        argv[0] = "@neomacsProgram@";
        execv(argv[0], argv);
        perror("Unable to launch Neomacs");
        return 127;
    }
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        [application setActivationPolicy:NSApplicationActivationPolicyAccessory];
        // NSApplication's delegate is weak; retain it throughout the event loop.
        __attribute__((objc_precise_lifetime)) NeomacsLauncher *launcher =
            [[NeomacsLauncher alloc] init];
        application.delegate = launcher;
        [application run];
    }
    return 0;
}
#endif
