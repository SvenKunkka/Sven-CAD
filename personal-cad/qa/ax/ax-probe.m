#import <ApplicationServices/ApplicationServices.h>
#import <Foundation/Foundation.h>

#include <errno.h>
#include <libproc.h>
#include <signal.h>
#include <sys/proc.h>
#include <sysexits.h>

static NSString *const Usage =
    @"Usage: ax-probe --pid PID --expect LABEL [--samples N] [--min-valid N]\n"
    @"                [--interval-ms N] [--role AXOutline|AXTable] [--identifier ID] [--window-title TITLE]\n"
    @"       ax-probe --pid PID --window-title TITLE --discover\n"
    @"Queries one selected row with an exact synthetic label. Writes JSON Lines to stdout.\n";

typedef NS_ENUM(NSInteger, ProbeStatus) {
    Unsupported,
    NoWindows,
    Empty,
    Mismatch,
    ScanLimit,
    QueryError,
    Matched,
    PermissionDenied,
    TargetExited,
    TargetChanged,
    TargetUninspectable,
};

static NSString *statusName(ProbeStatus status)
{
    switch (status) {
        case Unsupported: return @"unsupported";
        case NoWindows: return @"no_windows";
        case Empty: return @"empty";
        case Mismatch: return @"mismatch";
        case ScanLimit: return @"scan_limit";
        case QueryError: return @"query_error";
        case Matched: return @"matched";
        case PermissionDenied: return @"permission_denied";
        case TargetExited: return @"target_exited";
        case TargetChanged: return @"target_changed";
        case TargetUninspectable: return @"target_uninspectable";
    }
}

static ProbeStatus statusForAXError(AXError error)
{
    switch (error) {
        case kAXErrorAPIDisabled: return PermissionDenied;
        case kAXErrorNoValue: return Empty;
        case kAXErrorAttributeUnsupported:
        case kAXErrorNotImplemented: return Unsupported;
        default: return QueryError;
    }
}

static NSInteger integerOption(NSString *raw, NSInteger fallback, NSInteger min, NSInteger max)
{
    if (raw == nil) {
        return fallback;
    }
    NSScanner *scanner = [NSScanner scannerWithString:raw];
    scanner.charactersToBeSkipped = nil;
    NSInteger value;
    if (![scanner scanInteger:&value] || !scanner.isAtEnd || value < min || value > max) {
        return -1;
    }
    return value;
}

static NSDictionary<NSString *, NSString *> *parseOptions(int argc, const char *argv[])
{
    NSArray *allowed = @[@"--pid", @"--expect", @"--samples", @"--min-valid", @"--interval-ms",
                         @"--role", @"--identifier", @"--window-title"];
    NSMutableDictionary *options = [NSMutableDictionary dictionary];
    for (int index = 1; index < argc;) {
        NSString *key = @(argv[index++]);
        if (key == nil || options[key] != nil) {
            return nil;
        }
        if ([key isEqualToString:@"--discover"]) {
            options[key] = @"yes";
            continue;
        }
        if (![allowed containsObject:key] || index >= argc) {
            return nil;
        }
        NSString *value = @(argv[index++]);
        if (value.length == 0) {
            return nil;
        }
        options[key] = value;
    }
    NSString *role = options[@"--role"];
    if (role != nil && ![@[@"AXOutline", @"AXTable"] containsObject:role]) {
        return nil;
    }
    return options;
}

static void emit(NSString *event, NSDictionary *fields)
{
    NSMutableDictionary *record = [fields mutableCopy];
    record[@"event"] = event;
    record[@"unix_ms"] = @((long long)(NSDate.date.timeIntervalSince1970 * 1000));
    NSData *data = [NSJSONSerialization dataWithJSONObject:record options:NSJSONWritingSortedKeys error:NULL];
    if (data == nil || fwrite(data.bytes, 1, data.length, stdout) != data.length ||
        fputc('\n', stdout) == EOF || fflush(stdout) != 0) {
        fputs("Cannot write AX evidence to stdout.\n", stderr);
        exit(EX_IOERR);
    }
}

static ProbeStatus inspectProcess(pid_t pid, struct proc_bsdinfo *info)
{
    if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, info, sizeof(*info)) != sizeof(*info)) {
        return kill(pid, 0) == -1 && errno == ESRCH ? TargetExited : TargetUninspectable;
    }
    return info->pbi_status == SZOMB ? TargetExited : Matched;
}

static ProbeStatus checkProcess(pid_t pid, const struct proc_bsdinfo *initial)
{
    struct proc_bsdinfo info;
    ProbeStatus status = inspectProcess(pid, &info);
    if (status != Matched) {
        return status;
    }
    return info.pbi_start_tvsec == initial->pbi_start_tvsec &&
        info.pbi_start_tvusec == initial->pbi_start_tvusec ? Matched : TargetChanged;
}

static id readAttribute(AXUIElementRef element, CFStringRef attribute, AXError *error)
{
    CFTypeRef value = NULL;
    *error = AXUIElementCopyAttributeValue(element, attribute, &value);
    return CFBridgingRelease(value);
}

static NSArray *readElements(AXUIElementRef element, CFStringRef attribute, CFIndex limit, AXError *error)
{
    CFArrayRef values = NULL;
    *error = AXUIElementCopyAttributeValues(element, attribute, 0, limit, &values);
    NSArray *array = CFBridgingRelease(values);
    return *error == kAXErrorSuccess && array != nil ? array : @[];
}

static AXUIElementRef axElement(id value)
{
    if (value == nil || CFGetTypeID((__bridge CFTypeRef)value) != AXUIElementGetTypeID()) {
        return NULL;
    }
    return (__bridge AXUIElementRef)value;
}

static id jsonString(id value)
{
    return [value isKindOfClass:NSString.class] ? value : NSNull.null;
}

static NSString *matchingAttribute(AXUIElementRef selected, NSString *label, NSTimeInterval deadline)
{
    NSArray *attributes = @[(NSString *)kAXTitleAttribute, (NSString *)kAXValueAttribute,
                            (NSString *)kAXDescriptionAttribute];
    NSMutableArray *pending = [NSMutableArray arrayWithObject:(__bridge id)selected];
    for (NSUInteger index = 0; index < MIN(24, pending.count); index++) {
        if (NSProcessInfo.processInfo.systemUptime >= deadline) {
            break;
        }
        AXUIElementRef element = axElement(pending[index]);
        if (element == NULL) {
            continue;
        }
        for (NSString *attribute in attributes) {
            AXError error;
            id value = readAttribute(element, (__bridge CFStringRef)attribute, &error);
            if (error == kAXErrorSuccess && [value isKindOfClass:NSString.class] && [value isEqualToString:label]) {
                return attribute;
            }
        }
        AXError error;
        [pending addObjectsFromArray:readElements(element, kAXChildrenAttribute, 8, &error)];
    }
    return nil;
}

static ProbeStatus querySelection(AXUIElementRef element, NSString *label, NSTimeInterval deadline, NSMutableDictionary *record)
{
    // Populate native row/cell wrappers before reading selection, as in the upstream test.
    AXError error;
    NSArray *rows = readElements(element, kAXRowsAttribute, 1, &error);
    record[@"warm_rows_ax_error"] = @(error);
    record[@"warmed_rows"] = @(rows.count);
    AXUIElementRef first = axElement(rows.firstObject);
    if (first != NULL) {
        NSArray *cells = readElements(first, kAXChildrenAttribute, 8, &error);
        record[@"warm_cells_ax_error"] = @(error);
        record[@"warmed_cells"] = @(cells.count);
    }
    id value = readAttribute(element, kAXSelectedChildrenAttribute, &error);
    record[@"ax_error"] = @(error);
    if (error != kAXErrorSuccess) {
        return statusForAXError(error);
    }
    if (value == nil || CFGetTypeID((__bridge CFTypeRef)value) != CFArrayGetTypeID()) {
        return QueryError;
    }
    NSArray *selected = value;
    record[@"selected_count"] = @(selected.count);
    if (selected.count == 0) {
        return Empty;
    }
    AXUIElementRef item = axElement(selected.firstObject);
    if (selected.count != 1 || item == NULL) {
        return Mismatch;
    }
    NSString *attribute = matchingAttribute(item, label, deadline);
    if (attribute == nil) {
        return Mismatch;
    }
    record[@"matched_attribute"] = attribute;
    record[@"matched_label"] = label;
    return Matched;
}

static ProbeStatus scan(AXUIElementRef app, NSDictionary *options, NSMutableDictionary *record)
{
    AXError error;
    NSTimeInterval deadline = NSProcessInfo.processInfo.systemUptime + 2;
    NSArray *windows = readElements(app, kAXWindowsAttribute, 8, &error);
    if (error != kAXErrorSuccess) {
        record[@"ax_error"] = @(error);
        return error == kAXErrorNoValue ? NoWindows : statusForAXError(error);
    }
    NSString *windowTitle = options[@"--window-title"];
    NSMutableArray *pending = [NSMutableArray array];
    AXError titleError = kAXErrorSuccess;
    for (id window in windows) {
        AXUIElementRef element = axElement(window);
        if (element == NULL) {
            continue;
        }
        if (windowTitle == nil || [windowTitle isEqual:readAttribute(element, kAXTitleAttribute, &error)]) {
            [pending addObject:window];
        } else if (error == kAXErrorAPIDisabled || error == kAXErrorCannotComplete) {
            titleError = error;
        }
    }
    if (pending.count == 0) {
        if (titleError != kAXErrorSuccess) {
            record[@"ax_error"] = @(titleError);
            return statusForAXError(titleError);
        }
        return NoWindows;
    }
    BOOL discovery = options[@"--discover"] != nil;
    NSMutableArray *discovered = [NSMutableArray array];
    ProbeStatus best = Unsupported;
    for (NSUInteger index = 0; index < pending.count; index++) {
        if (index >= 200 || NSProcessInfo.processInfo.systemUptime >= deadline) {
            return best > ScanLimit ? best : ScanLimit;
        }
        AXUIElementRef element = axElement(pending[index]);
        if (element == NULL) {
            continue;
        }
        NSString *role = readAttribute(element, kAXRoleAttribute, &error);
        if (error == kAXErrorAPIDisabled) {
            record[@"ax_error"] = @(error);
            return PermissionDenied;
        }
        if (error == kAXErrorCannotComplete && best < QueryError) {
            best = QueryError;
            record[@"ax_error"] = @(error);
        }
        BOOL isTable = [@[@"AXTable", @"AXOutline"] containsObject:role ?: @""];
        if (discovery && error == kAXErrorSuccess) {
            NSString *identifier = readAttribute(element, kAXIdentifierAttribute, &error);
            NSString *title = readAttribute(element, kAXTitleAttribute, &error);
            [discovered addObject:@{@"role": jsonString(role), @"identifier": jsonString(identifier),
                                    @"title": jsonString(title)}];
            record[@"views"] = discovered;
        }
        if (isTable) {
            if (discovery) {
                continue;
            }
            NSString *expectedRole = options[@"--role"];
            NSString *identifier = options[@"--identifier"];
            if (expectedRole != nil && ![role isEqualToString:expectedRole]) {
                continue;
            }
            if (identifier != nil && ![identifier isEqual:readAttribute(element, kAXIdentifierAttribute, &error)]) {
                continue;
            }
            NSMutableDictionary *candidate = [NSMutableDictionary dictionaryWithObject:role forKey:@"role"];
            candidate[@"window_title"] = windowTitle;
            ProbeStatus status = querySelection(element, options[@"--expect"], deadline, candidate);
            if (status >= best) {
                best = status;
                [record setDictionary:candidate];
            }
            if (status >= Matched) {
                return status;
            }
            // Do not traverse every row while finding views.
            continue;
        }
        NSArray *children = readElements(element, kAXChildrenAttribute, 32, &error);
        if (error == kAXErrorAPIDisabled) {
            record[@"ax_error"] = @(error);
            return PermissionDenied;
        }
        if (error == kAXErrorCannotComplete && best < QueryError) {
            best = QueryError;
            record[@"ax_error"] = @(error);
        }
        [pending addObjectsFromArray:children];
    }
    return best;
}

static int finish(ProbeStatus status, pid_t pid, NSInteger samples, NSInteger minValid, NSDictionary *counts)
{
    NSInteger matched = [counts[@"matched"] integerValue];
    NSInteger samplesRun = 0;
    for (NSNumber *count in counts.allValues) {
        samplesRun += count.integerValue;
    }
    BOOL pass = status == Matched && matched >= minValid;
    emit(@"summary", @{
        @"pid": @(pid), @"status": statusName(status), @"pass": @(pass), @"samples_requested": @(samples),
        @"min_valid": @(minValid), @"samples_run": @(samplesRun), @"matched": @(matched), @"counts": counts,
    });
    if (pass) {
        return EX_OK;
    }
    if (status == PermissionDenied) {
        return 77;
    }
    return status >= TargetExited ? EX_UNAVAILABLE : 1;
}

int main(int argc, const char *argv[])
{
    @autoreleasepool {
        if (argc == 2 && strcmp(argv[1], "--help") == 0) {
            fputs(Usage.UTF8String, stdout);
            return EX_OK;
        }
        NSDictionary *options = parseOptions(argc, argv);
        pid_t pid = (pid_t)integerOption(options[@"--pid"], -1, 1, INT_MAX);
        NSInteger samples = integerOption(options[@"--samples"], 100, 1, 10000);
        NSInteger minValid = integerOption(options[@"--min-valid"], samples, 1, samples);
        NSInteger interval = integerOption(options[@"--interval-ms"], 100, 0, 60000);
        NSString *label = options[@"--expect"];
        BOOL discovery = options[@"--discover"] != nil;
        if (pid < 1 || samples < 1 || minValid < 1 || interval < 0 ||
            (discovery ? options[@"--window-title"] == nil : label == nil)) {
            fputs(Usage.UTF8String, stderr);
            emit(@"summary", @{@"status": @"invalid_arguments", @"pass": @NO});
            return EX_USAGE;
        }

        struct proc_bsdinfo initial = {0};
        ProbeStatus status = inspectProcess(pid, &initial);
        // This preflight neither requests permission nor opens a TCC prompt.
        BOOL trusted = AXIsProcessTrusted();
        emit(@"start", @{
            @"pid": @(pid), @"trusted": @(trusted), @"expected_label": label ?: NSNull.null, @"samples": @(samples),
            @"min_valid": @(minValid), @"interval_ms": @(interval),
            @"target_start_seconds": @(initial.pbi_start_tvsec),
            @"target_start_microseconds": @(initial.pbi_start_tvusec),
        });
        if (status != Matched || !trusted) {
            return finish(status == Matched ? PermissionDenied : status, pid, samples, minValid, @{});
        }
        AXUIElementRef systemWide = AXUIElementCreateSystemWide();
        AXError timeoutError = AXUIElementSetMessagingTimeout(systemWide, 0.25);
        CFRelease(systemWide);
        if (timeoutError != kAXErrorSuccess) {
            emit(@"setup_error", @{@"ax_error": @(timeoutError)});
            return finish(statusForAXError(timeoutError), pid, samples, minValid, @{});
        }

        AXUIElementRef app = AXUIElementCreateApplication(pid);
        if (discovery) {
            NSMutableDictionary *record = [NSMutableDictionary dictionary];
            ProbeStatus discoveryStatus = scan(app, options, record);
            CFRelease(app);
            ProbeStatus after = checkProcess(pid, &initial);
            if (after != Matched) {
                discoveryStatus = after;
                [record removeAllObjects];
            }
            record[@"pid"] = @(pid);
            record[@"window_title"] = options[@"--window-title"];
            BOOL found = [record[@"views"] count] > 0;
            record[@"status"] = found && discoveryStatus == Unsupported ? @"discovered" : statusName(discoveryStatus);
            emit(@"discovery", record);
            return found && discoveryStatus == Unsupported ? EX_OK : 1;
        }
        NSMutableDictionary<NSString *, NSNumber *> *counts = [NSMutableDictionary dictionary];
        ProbeStatus lastFailure = Mismatch;
        for (NSInteger index = 1; index <= samples; index++) {
            @autoreleasepool {
                NSTimeInterval started = NSProcessInfo.processInfo.systemUptime;
                NSMutableDictionary *record = [NSMutableDictionary dictionary];
                status = checkProcess(pid, &initial);
                if (status == Matched) {
                    status = scan(app, options, record);
                    ProbeStatus after = checkProcess(pid, &initial);
                    if (after != Matched) {
                        [record removeAllObjects];
                        status = after;
                    }
                }
                NSString *name = statusName(status);
                record[@"status"] = name;
                record[@"pid"] = @(pid);
                record[@"sample"] = @(index);
                record[@"elapsed_ms"] = @((NSInteger)((NSProcessInfo.processInfo.systemUptime - started) * 1000));
                emit(@"sample", record);
                counts[name] = @(counts[name].integerValue + 1);
                if (status > Matched) {
                    CFRelease(app);
                    return finish(status, pid, samples, minValid, counts);
                }
                if (status != Matched) {
                    lastFailure = status;
                }
            }
            if (index < samples && interval > 0) {
                [NSThread sleepForTimeInterval:(double)interval / 1000];
            }
        }
        CFRelease(app);
        status = [counts[@"matched"] integerValue] >= minValid ? Matched : lastFailure;
        return finish(status, pid, samples, minValid, counts);
    }
}
