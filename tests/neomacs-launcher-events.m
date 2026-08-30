#define NEOMACS_LAUNCHER_TEST
#import "neomacs-launcher.m"
#import <CoreServices/CoreServices.h>

// Exercise the production delegate without a WindowServer, real Neomacs,
// LaunchServices registration or the user's configuration.
@interface RecordingLauncher : NeomacsLauncher
@property(nonatomic, strong) NSMutableArray<NSArray<NSString *> *> *requests;
@property(nonatomic) NSUInteger errors;
@end

@implementation RecordingLauncher
- (instancetype)init {
    self = [super init];
    if (self) {
        _requests = [NSMutableArray array];
    }
    return self;
}
- (BOOL)startSession:(NSArray<NSString *> *)arguments {
    [self.requests addObject:arguments];
    return YES;
}
- (void)reportError:(NSError *)error {
    (void)error;
    self.errors++;
}
@end

@interface RecordingApplication : NSObject
@property(nonatomic) NSApplicationDelegateReply reply;
@property(nonatomic) BOOL terminated;
@end
@implementation RecordingApplication
- (void)replyToOpenOrPrint:(NSApplicationDelegateReply)reply { self.reply = reply; }
- (void)terminate:(id)sender { (void)sender; self.terminated = YES; }
@end

static void check(BOOL condition, const char *message) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        exit(1);
    }
}

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc == 3 && strcmp(argv[1], "--register") == 0) {
            NSURL *application = [NSURL fileURLWithPath:@(argv[2])];
            OSStatus status = LSRegisterURL((__bridge CFURLRef)application, true);
            if (status != noErr) {
                fprintf(stderr, "Test app registration failed: %d\n", (int)status);
                return 1;
            }
            return 0;
        }
        if (argc == 4 && strcmp(argv[1], "--can-open") == 0) {
            NSURL *application = [[NSURL fileURLWithPath:@(argv[2])] URLByResolvingSymlinksInPath];
            NSURL *file = [NSURL fileURLWithPath:@(argv[3])];
            NSArray<NSURL *> *candidates = [[NSWorkspace sharedWorkspace] URLsForApplicationsToOpenURL:file];
            for (NSURL *candidate in candidates) {
                if ([candidate.URLByResolvingSymlinksInPath.path isEqual:application.path]) {
                    return 0;
                }
            }
            NSString *identifier = [NSBundle bundleWithURL:application].bundleIdentifier;
            NSURL *registered = [[NSWorkspace sharedWorkspace] URLForApplicationWithBundleIdentifier:identifier];
            fprintf(stderr, "Test app is not an Open With candidate for %s (registered=%s, candidates=%lu)\n",
                    argv[3], registered ? "yes" : "no", (unsigned long)candidates.count);
            NSURL *textEdit = [[NSWorkspace sharedWorkspace] URLForApplicationWithBundleIdentifier:@"com.apple.TextEdit"];
            fprintf(stderr, "Probe: uid=%u file-exists=%s bundle=%s TextEdit=%s\n", getuid(),
                    [[NSFileManager defaultManager] fileExistsAtPath:file.path] ? "yes" : "no",
                    identifier.UTF8String ?: "missing", textEdit ? "visible" : "missing");
            return 1;
        }
        check(argc == 1, "unexpected test arguments");
        RecordingApplication *recording = [[RecordingApplication alloc] init];
        NSApplication *application = (NSApplication *)recording;
        RecordingLauncher *launcher = [[RecordingLauncher alloc] init];
        NSArray<NSString *> *files = @[@"/tmp/a file.nix", @"/tmp/中文.md", @"-option.el"];
        [launcher application:application openFiles:files];
        check([launcher.requests isEqual:@[@[@"--", @"/tmp/a file.nix", @"/tmp/中文.md", @"-option.el"]]],
              "file boundaries, Unicode and option separator");
        [launcher openUntitledIfNeeded];
        check(launcher.requests.count == 1, "no empty session after cold file event");
        check(recording.reply == NSApplicationDelegateReplySuccess, "successful open reply");
        [launcher application:application openFiles:@[@"/tmp/later.toml"]];
        check([launcher.requests.lastObject isEqual:@[@"--", @"/tmp/later.toml"]],
              "warm file event");
        [launcher application:application openURLs:@[[NSURL fileURLWithPath:@"/tmp/a%20b.md"]]];
        check([launcher.requests.lastObject isEqual:@[@"--", @"/tmp/a%20b.md"]],
              "file URLs decoded exactly once");
        NSUInteger before = launcher.requests.count;
        [launcher application:application openURLs:@[[NSURL URLWithString:@"https://example.invalid/a.md"]]];
        check(launcher.errors == 1 && launcher.requests.count == before,
              "non-file URLs rejected without launching a process");

        RecordingLauncher *empty = [[RecordingLauncher alloc] init];
        [empty applicationOpenUntitledFile:application];
        [empty openUntitledIfNeeded];
        check([empty.requests isEqual:@[@[]]], "one session on an ordinary app launch");

        RecordingLauncher *urlOnly = [[RecordingLauncher alloc] init];
        [urlOnly application:application openURLs:@[[NSURL fileURLWithPath:@"/tmp/file.nix"]]];
        [urlOnly openUntitledIfNeeded];
        check([urlOnly.requests isEqual:@[@[@"--", @"/tmp/file.nix"]]],
              "URL-only cold launch has no extra empty session");
        puts("Neomacs Apple-event delegate checks passed");
    }
    return 0;
}
